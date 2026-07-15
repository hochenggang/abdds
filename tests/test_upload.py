"""上传模块异步测试"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import httpx
import pytest
import respx

from abdds._http import _HttpTransport
from abdds.errors import BaiduPanAPIError
from abdds.models import UploadResult
from abdds.upload import (
    BLOCK_SIZE,
    _calculate_bytes_md5_blocks,
    _calculate_file_md5_blocks,
    _get_upload_server,
    upload,
)


@pytest.fixture
def transport() -> _HttpTransport:
    t = _HttpTransport(max_retries=0, timeout=(5, 10))
    t.set_access_token("test_token")
    return t


class TestCalculateFileMd5Blocks:
    async def test_small_file_single_block(self, tmp_path: Path):
        f = tmp_path / "small.bin"
        content = b"hello world"
        f.write_bytes(content)

        result = await _calculate_file_md5_blocks(f)
        assert len(result) == 1
        assert result[0] == hashlib.md5(content).hexdigest()

    async def test_large_file_multiple_blocks(self, tmp_path: Path):
        f = tmp_path / "large.bin"
        block1 = b"\x00" * BLOCK_SIZE
        block2 = b"\x01" * BLOCK_SIZE
        f.write_bytes(block1 + block2)

        result = await _calculate_file_md5_blocks(f)
        assert len(result) == 2
        assert result[0] == hashlib.md5(block1).hexdigest()
        assert result[1] == hashlib.md5(block2).hexdigest()

    async def test_exact_block_size(self, tmp_path: Path):
        f = tmp_path / "exact.bin"
        content = b"\xAA" * BLOCK_SIZE
        f.write_bytes(content)

        result = await _calculate_file_md5_blocks(f)
        assert len(result) == 1


class TestCalculateBytesMd5Blocks:
    def test_small_bytes(self):
        data = b"hello"
        result = _calculate_bytes_md5_blocks(data)
        assert len(result) == 1
        assert result[0] == hashlib.md5(data).hexdigest()

    def test_large_bytes(self):
        data = b"\x00" * (BLOCK_SIZE * 2 + 100)
        result = _calculate_bytes_md5_blocks(data)
        assert len(result) == 3
        assert result[0] == hashlib.md5(data[:BLOCK_SIZE]).hexdigest()
        assert result[1] == hashlib.md5(data[BLOCK_SIZE:BLOCK_SIZE*2]).hexdigest()
        assert result[2] == hashlib.md5(data[BLOCK_SIZE*2:]).hexdigest()


def _add_small_upload_mocks():
    """为小文件上传添加 mock 响应"""
    # precreate
    respx.post("https://pan.baidu.com/rest/2.0/xpan/file").mock(
        return_value=httpx.Response(200, json={"errno": 0, "uploadid": "uid123", "block_list": [0]})
    )
    # locate upload server
    respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
        return_value=httpx.Response(200, json={"errno": 0, "servers": [{"server": "https://upload.example.com"}]})
    )
    # direct upload
    respx.post("https://upload.example.com/rest/2.0/pcs/file").mock(
        return_value=httpx.Response(200, json={
            "errno": 0,
            "fs_id": 456,
            "md5": "md5val",
            "path": "/apps/test/small.txt",
            "size": 5,
        })
    )


class TestUploadWithPath:
    @respx.mock
    async def test_small_file(self, tmp_path: Path, transport: _HttpTransport):
        f = tmp_path / "small.txt"
        f.write_text("hello")

        _add_small_upload_mocks()

        result = await upload(transport, f, PurePosixPath("/small.txt"), "/apps/test")
        assert isinstance(result, UploadResult)
        assert result.fs_id == 456

    @respx.mock
    async def test_large_file(self, tmp_path: Path, transport: _HttpTransport):
        f = tmp_path / "large.bin"
        f.write_bytes(b"\x00" * (BLOCK_SIZE + 1))

        # precreate
        respx.post("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0, "uploadid": "uid456", "block_list": [0, 1]})
        )
        # locate upload server
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0, "servers": [{"server": "https://upload.example.com"}]})
        )
        # part uploads (2 parts)
        respx.post("https://upload.example.com/rest/2.0/pcs/superfile2").mock(
            return_value=httpx.Response(200, json={"errno": 0})
        )
        # merge - 注意: precreate 和 merge 都用 POST xpan/file，需要区分
        # respx 会按注册顺序匹配
        respx.post("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            side_effect=[
                httpx.Response(200, json={"errno": 0, "uploadid": "uid456", "block_list": [0, 1]}),
                httpx.Response(200, json={
                    "errno": 0,
                    "fs_id": 789,
                    "md5": "merged_md5",
                    "path": "/apps/test/large.bin",
                    "size": BLOCK_SIZE + 1,
                }),
            ]
        )

        result = await upload(transport, f, PurePosixPath("/large.bin"), "/apps/test")
        assert result.fs_id == 789

    async def test_file_not_found(self, transport: _HttpTransport):
        with pytest.raises(FileNotFoundError):
            await upload(transport, Path("/nonexistent/file.txt"), PurePosixPath("/file.txt"), "/apps/test")


class TestUploadWithBytes:
    @respx.mock
    async def test_small_bytes(self, transport: _HttpTransport):
        _add_small_upload_mocks()

        result = await upload(transport, b"hello", PurePosixPath("/small.txt"), "/apps/test")
        assert isinstance(result, UploadResult)
        assert result.fs_id == 456

    @respx.mock
    async def test_large_bytes(self, transport: _HttpTransport):
        data = b"\x00" * (BLOCK_SIZE + 1)

        # precreate + merge
        respx.post("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            side_effect=[
                httpx.Response(200, json={"errno": 0, "uploadid": "uid456", "block_list": [0, 1]}),
                httpx.Response(200, json={"errno": 0, "fs_id": 789, "md5": "m", "path": "/apps/test/f", "size": BLOCK_SIZE + 1}),
            ]
        )
        # locate upload server
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0, "servers": [{"server": "https://upload.example.com"}]})
        )
        # part uploads
        respx.post("https://upload.example.com/rest/2.0/pcs/superfile2").mock(
            return_value=httpx.Response(200, json={"errno": 0})
        )

        result = await upload(transport, data, PurePosixPath("/f"), "/apps/test")
        assert result.fs_id == 789


class TestUploadWithGenerator:
    @respx.mock
    async def test_small_generator(self, transport: _HttpTransport):
        _add_small_upload_mocks()

        def gen():
            yield b"hello"

        result = await upload(transport, gen(), PurePosixPath("/small.txt"), "/apps/test")
        assert isinstance(result, UploadResult)

    @respx.mock
    async def test_small_async_generator(self, transport: _HttpTransport):
        _add_small_upload_mocks()

        async def gen():
            yield b"hello"

        result = await upload(transport, gen(), PurePosixPath("/small.txt"), "/apps/test")
        assert isinstance(result, UploadResult)

    @respx.mock
    async def test_multi_chunk_async_generator(self, transport: _HttpTransport):
        # 生成超过 BLOCK_SIZE 的数据
        respx.post("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            side_effect=[
                httpx.Response(200, json={"errno": 0, "uploadid": "uid", "block_list": [0, 1]}),
                httpx.Response(200, json={"errno": 0, "fs_id": 999, "md5": "m", "path": "/apps/test/f", "size": BLOCK_SIZE + 100}),
            ]
        )
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0, "servers": [{"server": "https://upload.example.com"}]})
        )
        respx.post("https://upload.example.com/rest/2.0/pcs/superfile2").mock(
            return_value=httpx.Response(200, json={"errno": 0})
        )

        async def gen():
            yield b"\x00" * BLOCK_SIZE
            yield b"\x01" * 100

        result = await upload(transport, gen(), PurePosixPath("/f"), "/apps/test")
        assert result.fs_id == 999


class TestUploadInvalidType:
    async def test_invalid_file_from_type(self, transport: _HttpTransport):
        with pytest.raises(TypeError, match="file_from must be"):
            await upload(transport, "invalid_string", PurePosixPath("/f"), "/apps/test")  # type: ignore


class TestUploadEmptyData:
    async def test_empty_file_raises(self, transport: _HttpTransport, tmp_path: Path):
        """上传空文件 (0 bytes) 应抛 ValueError"""
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        with pytest.raises(ValueError, match="Cannot upload empty file"):
            await upload(transport, f, PurePosixPath("/empty.bin"), "/apps/test")

    async def test_empty_bytes_raises(self, transport: _HttpTransport):
        """上传空 bytes 应抛 ValueError"""
        with pytest.raises(ValueError, match="Cannot upload empty data"):
            await upload(transport, b"", PurePosixPath("/empty.bin"), "/apps/test")

    async def test_empty_generator_raises(self, transport: _HttpTransport):
        """上传空生成器应抛 ValueError"""
        def empty_gen():
            return
            yield  # 使其成为生成器

        with pytest.raises(ValueError, match="Cannot upload empty generator"):
            await upload(transport, empty_gen(), PurePosixPath("/empty.bin"), "/apps/test")

    async def test_empty_async_generator_raises(self, transport: _HttpTransport):
        """上传空异步生成器应抛 ValueError"""
        async def empty_gen():
            return
            yield  # 使其成为异步生成器

        with pytest.raises(ValueError, match="Cannot upload empty async generator"):
            await upload(transport, empty_gen(), PurePosixPath("/empty.bin"), "/apps/test")


class TestGetUploadServer:
    @respx.mock
    async def test_prefer_https(self, transport: _HttpTransport):
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={
                "errno": 0,
                "servers": [
                    {"server": "http://upload1.example.com"},
                    {"server": "https://upload2.example.com"},
                ],
            })
        )

        host = await _get_upload_server(transport, "/apps/test/f.txt", "uid")
        assert host == "https://upload2.example.com"

    @respx.mock
    async def test_no_https_available(self, transport: _HttpTransport):
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={
                "errno": 0,
                "servers": [{"server": "http://upload1.example.com"}],
            })
        )

        host = await _get_upload_server(transport, "/apps/test/f.txt", "uid")
        assert host == "http://upload1.example.com"

    @respx.mock
    async def test_no_server_raises(self, transport: _HttpTransport):
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0, "servers": []})
        )

        with pytest.raises(BaiduPanAPIError) as exc_info:
            await _get_upload_server(transport, "/apps/test/f.txt", "uid")
        assert "No upload server" in str(exc_info.value)

    @respx.mock
    async def test_server_missing_key_raises(self, transport: _HttpTransport):
        """servers 列表中的条目缺少 server 字段应报错"""
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0, "servers": [{"port": 443}]})
        )

        with pytest.raises(BaiduPanAPIError) as exc_info:
            await _get_upload_server(transport, "/apps/test/f.txt", "uid")
        assert "No valid upload server" in str(exc_info.value)
