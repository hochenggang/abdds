"""上传相关逻辑 - 异步小文件单步上传 & 大文件分片上传"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections.abc import AsyncGenerator as _AsyncGeneratorABC
from collections.abc import Generator as _GeneratorABC
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, AsyncGenerator, Generator

import aiofiles
import aiofiles.os

from .errors import BaiduPanAPIError
from .models import _ApiPreUploadResponse, UploadResult

if TYPE_CHECKING:
    from ._http import _HttpTransport

logger = logging.getLogger("abdds")

BLOCK_SIZE = 4 * 1024 * 1024  # 4MB

# file_from 参数可接受类型
UploadSource = Path | bytes | Generator[bytes, None, None] | AsyncGenerator[bytes, None]


def _is_generator(obj: object) -> bool:
    """判断对象是否为同步生成器"""
    return isinstance(obj, _GeneratorABC) and not isinstance(obj, _AsyncGeneratorABC)


def _is_async_generator(obj: object) -> bool:
    """判断对象是否为异步生成器"""
    return isinstance(obj, _AsyncGeneratorABC)


async def upload(
    transport: _HttpTransport,
    file_from: UploadSource,
    to: PurePosixPath,
    pan_dir_base: str,
) -> UploadResult:
    """
    异步上传数据到百度网盘

    - 小文件 (<=4MB): 单步上传 (precreate + direct upload)
    - 大文件 (>4MB):  分片上传 (precreate + 分片 + merge)

    Args:
        transport: 异步 HTTP 传输层
        file_from: 数据来源，支持 Path / bytes / Generator[bytes] / AsyncGenerator[bytes]
        to: 远程路径 (PurePosixPath)，将拼接在 /apps/{app_name} 之后
        pan_dir_base: 应用目录前缀，如 /apps/myapp

    Raises:
        TypeError: file_from 类型不正确
    """
    # 类型校验
    if (
        not isinstance(file_from, (Path, bytes))
        and not _is_generator(file_from)
        and not _is_async_generator(file_from)
    ):
        raise TypeError(
            f"file_from must be Path, bytes, Generator[bytes], or AsyncGenerator[bytes], "
            f"got {type(file_from).__name__}"
        )

    # 构造完整远程路径: pan_dir_base + to
    remote_path = f"{pan_dir_base}{to.as_posix()}"

    # 根据 file_from 类型提取数据
    if isinstance(file_from, Path):
        if not file_from.is_file():
            raise FileNotFoundError(f"File not found: {file_from}")
        file_size = (await aiofiles.os.stat(file_from)).st_size
        if file_size == 0:
            raise ValueError("Cannot upload empty file (0 bytes)")
        block_md5_list = await _calculate_file_md5_blocks(file_from)
    elif isinstance(file_from, bytes):
        file_size = len(file_from)
        if file_size == 0:
            raise ValueError("Cannot upload empty data (0 bytes)")
        block_md5_list = _calculate_bytes_md5_blocks(file_from)
    elif _is_async_generator(file_from):
        # 异步生成器需要消费一次来计算大小和 MD5，缓存在内存中
        chunks: list[bytes] = []
        file_size = 0
        md5_list: list[str] = []
        current_chunk = io.BytesIO()

        async for data in file_from:
            current_chunk.write(data)
            file_size += len(data)
            if current_chunk.tell() >= BLOCK_SIZE:
                block_data = current_chunk.getvalue()
                chunks.append(block_data)
                md5_list.append(hashlib.md5(block_data).hexdigest())
                current_chunk = io.BytesIO()

        # 处理最后不足一个 BLOCK_SIZE 的数据
        remaining = current_chunk.getvalue()
        if remaining:
            chunks.append(remaining)
            md5_list.append(hashlib.md5(remaining).hexdigest())

        block_md5_list = md5_list

        if file_size == 0:
            raise ValueError("Cannot upload empty async generator (0 bytes yielded)")

        # 将 chunks 重新组装为异步生成器供上传使用
        async def _replay_chunks() -> AsyncGenerator[bytes, None]:
            for chunk in chunks:
                yield chunk

        file_from = _replay_chunks()
    elif _is_generator(file_from):
        # 同步生成器
        chunks2: list[bytes] = []
        file_size = 0
        md5_list2: list[str] = []
        current_chunk2 = io.BytesIO()

        for data in file_from:
            current_chunk2.write(data)
            file_size += len(data)
            if current_chunk2.tell() >= BLOCK_SIZE:
                block_data = current_chunk2.getvalue()
                chunks2.append(block_data)
                md5_list2.append(hashlib.md5(block_data).hexdigest())
                current_chunk2 = io.BytesIO()

        remaining2 = current_chunk2.getvalue()
        if remaining2:
            chunks2.append(remaining2)
            md5_list2.append(hashlib.md5(remaining2).hexdigest())

        block_md5_list = md5_list2

        if file_size == 0:
            raise ValueError("Cannot upload empty generator (0 bytes yielded)")

        file_from = (chunk for chunk in chunks2)

    logger.info("Uploading -> %s (%d bytes)", remote_path, file_size)

    # 预上传
    pre_resp = await _api_pre_create(transport, remote_path, file_size, block_md5_list)

    # 获取上传服务器
    upload_host = await _get_upload_server(transport, remote_path, pre_resp.uploadid)

    if file_size <= BLOCK_SIZE:
        return await _upload_small(transport, upload_host, pre_resp, file_from, file_size)
    else:
        return await _upload_multipart(transport, upload_host, pre_resp, file_from, block_md5_list)


async def _upload_small(
    transport: _HttpTransport,
    host: str,
    pre_resp: _ApiPreUploadResponse,
    file_from: UploadSource,
    file_size: int,
) -> UploadResult:
    """小文件单步上传"""
    url = f"{host}/rest/2.0/pcs/file"
    params = {"method": "upload", "path": pre_resp.path}

    if isinstance(file_from, Path):
        async with aiofiles.open(file_from, "rb") as f:
            data = await f.read()
    elif isinstance(file_from, bytes):
        data = file_from
    elif _is_async_generator(file_from):
        chunks: list[bytes] = []
        async for chunk in file_from:
            chunks.append(chunk)
        data = b"".join(chunks)
    elif _is_generator(file_from):
        data = b"".join(file_from)

    files = {"file": ("blob", data)}
    resp = await transport.request("POST", url, params=params, files=files)
    return UploadResult(
        fs_id=resp.get("fs_id"),
        md5=resp.get("md5"),
        path=resp.get("path"),
        size=resp.get("size"),
    )


async def _upload_multipart(
    transport: _HttpTransport,
    host: str,
    pre_resp: _ApiPreUploadResponse,
    file_from: UploadSource,
    block_md5_list: list[str],
) -> UploadResult:
    """大文件分片上传"""
    url_prefix = f"{host}/rest/2.0/pcs/superfile2"

    if isinstance(file_from, Path):
        async with aiofiles.open(file_from, "rb") as f:
            for idx in range(len(block_md5_list)):
                await f.seek(idx * BLOCK_SIZE)
                chunk_data = await f.read(BLOCK_SIZE)
                await _upload_part(transport, url_prefix, pre_resp, idx, chunk_data)
    elif isinstance(file_from, bytes):
        for idx in range(len(block_md5_list)):
            chunk_data = file_from[idx * BLOCK_SIZE : (idx + 1) * BLOCK_SIZE]
            await _upload_part(transport, url_prefix, pre_resp, idx, chunk_data)
    elif _is_async_generator(file_from):
        idx = 0
        async for chunk_data in file_from:
            await _upload_part(transport, url_prefix, pre_resp, idx, chunk_data)
            idx += 1
    elif _is_generator(file_from):
        for idx, chunk_data in enumerate(file_from):
            await _upload_part(transport, url_prefix, pre_resp, idx, chunk_data)

    # 合并分片
    return await _api_create_file(
        transport, pre_resp.path, pre_resp.uploadid, pre_resp.file_size, block_md5_list
    )


async def _upload_part(
    transport: _HttpTransport,
    url_prefix: str,
    pre_resp: _ApiPreUploadResponse,
    partseq: int,
    chunk_data: bytes,
) -> None:
    """上传单个分片"""
    logger.debug("Uploading part %d", partseq + 1)
    params = {
        "method": "upload",
        "type": "tmpfile",
        "path": pre_resp.path,
        "uploadid": pre_resp.uploadid,
        "partseq": str(partseq),
    }
    files = {"file": ("blob", chunk_data)}
    await transport.request("POST", url_prefix, params=params, files=files)


async def _api_pre_create(
    transport: _HttpTransport,
    path: str,
    size: int,
    block_list: list[str],
) -> _ApiPreUploadResponse:
    """预上传 - 通知云端新建上传任务"""
    params = {"method": "precreate"}
    data = {
        "path": path,
        "size": size,
        "isdir": "0",
        "autoinit": "1",
        "rtype": "3",  # 覆盖
        "block_list": json.dumps(block_list),
    }
    resp = await transport.request("POST", "https://pan.baidu.com/rest/2.0/xpan/file", params=params, data=data)
    return _ApiPreUploadResponse(
        path=path,
        uploadid=resp["uploadid"],
        block_list=resp.get("block_list", []),
        block_list_md5=block_list,
        file_size=size,
    )


async def _get_upload_server(transport: _HttpTransport, path: str, uploadid: str) -> str:
    """获取上传服务器域名"""
    params = {
        "method": "locateupload",
        "appid": "250528",
        "path": path,
        "uploadid": uploadid,
        "upload_version": "2.0",
    }
    resp = await transport.request("GET", "https://pan.baidu.com/rest/2.0/xpan/file", params=params)
    servers = resp.get("servers", [])
    if not servers:
        raise BaiduPanAPIError(-1, "No upload server available")

    for s in servers:
        server_url = s.get("server", "")
        if server_url.startswith("https"):
            return server_url

    # 降级：返回第一个有效 server
    for s in servers:
        server_url = s.get("server", "")
        if server_url:
            return server_url

    raise BaiduPanAPIError(-1, "No valid upload server in response")


async def _api_create_file(
    transport: _HttpTransport,
    path: str,
    uploadid: str,
    size: int,
    block_list: list[str],
) -> UploadResult:
    """合并分片完成上传"""
    params = {"method": "create"}
    data = {
        "path": path,
        "size": size,
        "isdir": "0",
        "rtype": "3",
        "uploadid": uploadid,
        "block_list": json.dumps(block_list),
    }
    resp = await transport.request("POST", "https://pan.baidu.com/rest/2.0/xpan/file", params=params, data=data)
    return UploadResult(
        fs_id=resp.get("fs_id"),
        md5=resp.get("md5"),
        path=resp.get("path"),
        size=resp.get("size"),
    )


async def _calculate_file_md5_blocks(file_path: Path) -> list[str]:
    """异步计算文件分片 MD5"""
    file_size = (await aiofiles.os.stat(file_path)).st_size

    async with aiofiles.open(file_path, "rb") as f:
        if file_size <= BLOCK_SIZE:
            content = await f.read()
            return [hashlib.md5(content).hexdigest()]

        md5_list: list[str] = []
        while True:
            chunk = await f.read(BLOCK_SIZE)
            if not chunk:
                break
            md5_list.append(hashlib.md5(chunk).hexdigest())

    return md5_list


def _calculate_bytes_md5_blocks(data: bytes) -> list[str]:
    """计算 bytes 对象的分片 MD5"""
    if len(data) <= BLOCK_SIZE:
        return [hashlib.md5(data).hexdigest()]

    md5_list: list[str] = []
    for i in range(0, len(data), BLOCK_SIZE):
        md5_list.append(hashlib.md5(data[i : i + BLOCK_SIZE]).hexdigest())
    return md5_list
