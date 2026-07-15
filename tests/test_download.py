"""下载模块异步测试"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from abdds._http import _HttpTransport
from abdds.download import download
from abdds.errors import BaiduPanNetworkError


DLINK = "https://dlink.example.com/file"


@pytest.fixture
def transport() -> _HttpTransport:
    t = _HttpTransport(max_retries=0, timeout=(5, 10))
    t.set_access_token("test_token")
    return t


class TestDownloadStream:
    @respx.mock
    async def test_file_to_none_returns_async_generator(self, transport: _HttpTransport):
        content = b"hello world data"
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=content)
        )

        result = await download(transport, DLINK, file_to=None)
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert b"".join(chunks) == content

    @respx.mock
    async def test_empty_file(self, transport: _HttpTransport):
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=b"")
        )

        result = await download(transport, DLINK, file_to=None)
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert b"".join(chunks) == b""

    @respx.mock
    async def test_retry_on_failure(self, transport: _HttpTransport):
        content = b"retry data"
        route = respx.get(DLINK)
        route.side_effect = [
            httpx.ConnectError("lost"),
            httpx.Response(200, content=content),
        ]

        with patch("abdds.download.asyncio.sleep", new_callable=AsyncMock):
            result = await download(transport, DLINK, file_to=None, max_retries=3)
            chunks = []
            async for chunk in result:
                chunks.append(chunk)

        assert b"".join(chunks) == content

    @respx.mock
    async def test_max_retries_exceeded(self, transport: _HttpTransport):
        respx.get(DLINK).mock(
            side_effect=httpx.ConnectError("lost")
        )

        with patch("abdds.download.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(BaiduPanNetworkError) as exc_info:
                result = await download(transport, DLINK, file_to=None, max_retries=2)
                chunks = []
                async for chunk in result:
                    chunks.append(chunk)

        assert "Download failed" in str(exc_info.value)


class TestDownloadFile:
    @respx.mock
    async def test_download_to_path(self, transport: _HttpTransport, tmp_path: Path):
        content = b"file content here"
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=content)
        )

        local_path = tmp_path / "downloaded.bin"
        result = await download(transport, DLINK, file_to=local_path)

        assert result == local_path
        assert local_path.read_bytes() == content

    @respx.mock
    async def test_creates_parent_dirs(self, transport: _HttpTransport, tmp_path: Path):
        content = b"nested file"
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=content)
        )

        local_path = tmp_path / "sub" / "dir" / "file.bin"
        result = await download(transport, DLINK, file_to=local_path)
        assert local_path.read_bytes() == content

    async def test_file_to_str_raises_typeerror(self, transport: _HttpTransport):
        """file_to 只接受 Path 或 None，str 应报 TypeError"""
        with pytest.raises(TypeError, match="file_to must be Path or None"):
            await download(transport, DLINK, file_to="/tmp/out.bin")  # type: ignore

    async def test_invalid_chunk_size_raises(self, transport: _HttpTransport):
        """chunk_size <= 0 应报 ValueError"""
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            await download(transport, DLINK, file_to=None, chunk_size=0)

        with pytest.raises(ValueError, match="chunk_size must be positive"):
            await download(transport, DLINK, file_to=None, chunk_size=-1)
