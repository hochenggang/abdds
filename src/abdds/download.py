"""下载相关逻辑 - 异步流式下载、断点续传"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator

import aiofiles
import httpx

from ._http import UserAgent
from .errors import BaiduPanNetworkError

if TYPE_CHECKING:
    from ._http import _HttpTransport

logger = logging.getLogger("abdds")


async def download(
    transport: _HttpTransport,
    dlink: str,
    file_to: Path | None = None,
    chunk_size: int = 1024 * 1024,
    max_retries: int = 5,
) -> AsyncGenerator[bytes, None] | Path:
    """
    异步下载文件

    - file_to=None: 返回异步字节迭代器 (流式下载)
    - file_to=Path: 下载到本地文件，返回 Path

    Args:
        transport: 异步 HTTP 传输层
        dlink: 下载链接
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
) -> AsyncGenerator[bytes, None]:
    """
    通过 dlink 异步流式下载文件，支持断点续传和指数重试
    """
    downloaded_bytes = 0
    retry_count = 0

    while True:
        try:
            headers = {"User-Agent": UserAgent}

            if downloaded_bytes > 0:
                headers["Range"] = f"bytes={downloaded_bytes}-"
                logger.info("Resuming download from byte %d...", downloaded_bytes)

            async with transport.client.stream(
                "GET",
                dlink,
                headers=headers,
                params={"access_token": transport._access_token},
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
