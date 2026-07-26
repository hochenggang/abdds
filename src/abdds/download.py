"""下载相关逻辑 - 异步流式下载、断点续传"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiofiles
import httpx

from .errors import BaiduPanNetworkError

if TYPE_CHECKING:
    from ._http import _HttpTransport

logger = logging.getLogger("abdds")

# 百度网盘下载接口要求 User-Agent 必须为 pan.baidu.com，否则触发防盗链错误 31326
DOWNLOAD_USER_AGENT = "pan.baidu.com"


def _build_download_url(dlink: str, access_token: str) -> str:
    """
    安全地将 access_token 追加到 dlink URL 的查询参数中。

    dlink 自带大量签名查询参数（fid, sign, expires 等），
    此函数解析现有参数，若已含 access_token 则跳过，否则追加。
    """
    parsed = urlparse(dlink)
    existing_params = parse_qs(parsed.query, keep_blank_values=True)

    # 若 dlink 已含 access_token 则不覆盖
    if "access_token" not in existing_params:
        existing_params["access_token"] = [access_token]

    # 重新拼接 query string（取每个参数的第一个值）
    new_query = urlencode({k: v[0] for k, v in existing_params.items()})
    return urlunparse(parsed._replace(query=new_query))


def _format_range(start: int | None, end: int | None) -> str | None:
    """
    将 (start, end) 元组格式化为 HTTP Range header 值。

        (0, 499)    → "bytes=0-499"
        (500, None) → "bytes=500-"
        (None, 500) → "bytes=-500"
        (None, None)→ None
    """
    if start is not None and end is not None:
        return f"bytes={start}-{end}"
    if start is not None:
        return f"bytes={start}-"
    if end is not None:
        return f"bytes=-{end}"
    return None


async def _download(
    transport: _HttpTransport,
    dlink: str,
    file_to: Path | None = None,
    chunk_size: int = 1024 * 1024,
    max_retries: int = 5,
) -> AsyncGenerator[bytes, None] | Path:
    """
    下载文件（内部接口）

    - file_to=None: 返回异步字节迭代器 (流式下载)
    - file_to=Path: 下载到本地文件，返回 Path

    Args:
        transport: 异步 HTTP 传输层
        dlink: 下载链接 (通过 file_metas 获取)
        file_to: 本地保存路径 (Path)，为 None 时返回迭代器
        chunk_size: 分块大小，默认 1MB
        max_retries: 最大重试次数，默认 5

    Returns:
        AsyncGenerator[bytes] 当 file_to=None，Path 当 file_to 指定路径

    Raises:
        TypeError: file_to 类型不正确
    """
    if file_to is not None and not isinstance(file_to, Path):
        raise TypeError(
            f"file_to must be Path or None, got {type(file_to).__name__}"
        )
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")

    stream = _download_stream(transport, dlink, chunk_size, max_retries)

    if file_to is None:
        return stream

    file_to.parent.mkdir(parents=True, exist_ok=True)

    async with aiofiles.open(file_to, "wb") as f:
        async for chunk in stream:
            await f.write(chunk)

    logger.info("Downloaded to %s", file_to)
    return file_to


async def _download_stream(
    transport: _HttpTransport,
    dlink: str,
    chunk_size: int = 1024 * 1024,
    max_retries: int = 5,
    range: tuple[int | None, int | None] | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    通过 dlink 异步流式下载文件，支持断点续传和指数重试。

    Args:
        range: 下载范围元组 (start, end)，None 表示不限制。
            (500, None) → bytes=500-   从 500 到末尾
            (0, 499)    → bytes=0-499  前 500 字节
            (None, 500) → bytes=-500   最后 500 字节
            下载中断后自动基于已下载位置重试。
    """
    start_byte, end_byte = range if range else (None, None)
    downloaded_bytes = start_byte if start_byte is not None else 0
    has_range = range is not None
    retry_count = 0

    while True:
        try:
            headers = {"User-Agent": DOWNLOAD_USER_AGENT}

            if retry_count == 0 and has_range:
                # 首次请求：使用用户指定的 range
                range_header = _format_range(start_byte, end_byte)
                if range_header:
                    headers["Range"] = range_header
            elif has_range or downloaded_bytes > 0:
                # 重试时：基于已下载位置计算 range
                range_header = _format_range(downloaded_bytes, end_byte)
                if range_header:
                    headers["Range"] = range_header

            if downloaded_bytes > 0:
                logger.info("Resuming download from byte %d...", downloaded_bytes)

            url = _build_download_url(dlink, transport._access_token)
            async with transport.client.stream(
                "GET",
                url,
                headers=headers,
                timeout=httpx.Timeout(60, connect=15),
            ) as response:
                response.raise_for_status()

                # 请求了 Range 但服务器返回 200，说明不支持断点续传
                if downloaded_bytes > 0 and response.status_code == 200:
                    logger.warning("Server does not support Range, restarting from beginning")
                    downloaded_bytes = 0

                async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                    if chunk:
                        yield chunk
                        downloaded_bytes += len(chunk)

            break  # 下载完成

        except httpx.RequestError as e:
            retry_count += 1
            if retry_count > max_retries:
                raise BaiduPanNetworkError(
                    f"Download failed after {max_retries} retries "
                    f"({downloaded_bytes} bytes downloaded): {e}",
                    url=dlink,
                ) from e

            wait_time = 2 ** (retry_count - 1)
            logger.warning(
                "Download interrupted at %d bytes: %s. Retrying in %ds...",
                downloaded_bytes, e, wait_time,
            )
            await asyncio.sleep(wait_time)
