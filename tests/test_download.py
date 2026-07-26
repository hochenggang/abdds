"""下载模块异步测试"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from abdds._http import _HttpTransport
from abdds.download import (
    DOWNLOAD_USER_AGENT,
    _build_download_url,
    _download,
    _download_stream,
    _format_range,
)
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

        result = await _download(transport, DLINK, file_to=None)
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert b"".join(chunks) == content

    @respx.mock
    async def test_empty_file(self, transport: _HttpTransport):
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=b"")
        )

        result = await _download(transport, DLINK, file_to=None)
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
            result = await _download(transport, DLINK, file_to=None, max_retries=3)
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
                result = await _download(transport, DLINK, file_to=None, max_retries=2)
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
        result = await _download(transport, DLINK, file_to=local_path)

        assert result == local_path
        assert local_path.read_bytes() == content

    @respx.mock
    async def test_creates_parent_dirs(self, transport: _HttpTransport, tmp_path: Path):
        content = b"nested file"
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=content)
        )

        local_path = tmp_path / "sub" / "dir" / "file.bin"
        result = await _download(transport, DLINK, file_to=local_path)
        assert local_path.read_bytes() == content

    async def test_file_to_str_raises_typeerror(self, transport: _HttpTransport):
        """file_to 只接受 Path 或 None，str 应报 TypeError"""
        with pytest.raises(TypeError, match="file_to must be Path or None"):
            await _download(transport, DLINK, file_to="/tmp/out.bin")  # type: ignore

    async def test_invalid_chunk_size_raises(self, transport: _HttpTransport):
        """chunk_size <= 0 应报 ValueError"""
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            await _download(transport, DLINK, file_to=None, chunk_size=0)

        with pytest.raises(ValueError, match="chunk_size must be positive"):
            await _download(transport, DLINK, file_to=None, chunk_size=-1)


class TestUserAgent:
    @respx.mock
    async def test_user_agent_is_pan_baidu_com(self, transport: _HttpTransport):
        """下载请求的 User-Agent 必须为 pan.baidu.com"""
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=b"data")
        )

        result = await _download(transport, DLINK, file_to=None)
        async for _ in result:
            pass

        assert DOWNLOAD_USER_AGENT == "pan.baidu.com"
        assert respx.calls[0].request.headers["User-Agent"] == "pan.baidu.com"


class TestBuildDownloadUrl:
    def test_append_access_token_to_clean_url(self):
        url = _build_download_url("https://dlink.example.com/file", "my_token")
        assert "access_token=my_token" in url

    def test_append_access_token_to_url_with_existing_params(self):
        url = _build_download_url(
            "https://dlink.example.com/file?fid=123&sign=abc", "my_token"
        )
        assert "access_token=my_token" in url
        assert "fid=123" in url
        assert "sign=abc" in url

    def test_no_duplicate_access_token(self):
        url = _build_download_url(
            "https://dlink.example.com/file?access_token=existing", "new_token"
        )
        # 不应覆盖已有的 access_token
        assert "access_token=existing" in url
        assert "new_token" not in url

    def test_download_with_query_param_dlink(self):
        """dlink 自带大量查询参数时 access_token 应安全追加"""
        dlink = "https://dlink.example.com/file?fid=123&sign=abc&expires=999"
        url = _build_download_url(dlink, "tok")
        assert "fid=123" in url
        assert "sign=abc" in url
        assert "expires=999" in url
        assert "access_token=tok" in url


class TestFormatRange:
    """验证 _format_range 元组转 Range header"""

    def test_start_only(self):
        assert _format_range(500, None) == "bytes=500-"

    def test_start_and_end(self):
        assert _format_range(0, 499) == "bytes=0-499"
        assert _format_range(500, 999) == "bytes=500-999"

    def test_end_only(self):
        assert _format_range(None, 500) == "bytes=-500"

    def test_both_none(self):
        assert _format_range(None, None) is None


class TestRangeDownload:
    """验证 _download_stream 的 range 参数支持断点续传/分片下载"""

    @respx.mock
    async def test_range_first_request(self, transport: _HttpTransport):
        """首次请求携带用户指定的 Range header"""
        respx.get(DLINK).mock(
            return_value=httpx.Response(206, content=b"partial data")
        )

        chunks = []
        async for chunk in _download_stream(transport, DLINK, range=(500, None)):
            chunks.append(chunk)

        assert respx.calls[0].request.headers["Range"] == "bytes=500-"
        assert b"".join(chunks) == b"partial data"

    @respx.mock
    async def test_range_with_end_byte(self, transport: _HttpTransport):
        """(500, 999) 格式：首次请求携带 range"""
        respx.get(DLINK).mock(
            return_value=httpx.Response(206, content=b"data")
        )

        async for _ in _download_stream(transport, DLINK, range=(500, 999)):
            pass

        assert respx.calls[0].request.headers["Range"] == "bytes=500-999"

    @respx.mock
    async def test_range_server_not_supported(self, transport: _HttpTransport):
        """服务器返回 200（非 206）时从头下载"""
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=b"full content")
        )

        chunks = []
        async for chunk in _download_stream(transport, DLINK, range=(500, None)):
            chunks.append(chunk)
        assert b"".join(chunks) == b"full content"

    @respx.mock
    async def test_range_retry_preserves_end_byte(self, transport: _HttpTransport):
        """带 end 的 range 在重试时保留 end 字节"""
        route = respx.get(DLINK)
        route.side_effect = [
            httpx.ConnectError("lost"),
            httpx.Response(206, content=b"partial"),
        ]

        with patch("abdds.download.asyncio.sleep", new_callable=AsyncMock):
            async for _ in _download_stream(transport, DLINK, range=(0, 99), max_retries=3):
                pass

        # 首次请求用用户 range
        assert respx.calls[0].request.headers["Range"] == "bytes=0-99"
        # 重试时 range 仍保留 end（0 字节已下载，range 仍为 bytes=0-99）
        assert "Range" in respx.calls[1].request.headers

    @respx.mock
    async def test_no_range_no_range_header(self, transport: _HttpTransport):
        """不指定 range 时不发送 Range header"""
        respx.get(DLINK).mock(
            return_value=httpx.Response(200, content=b"data")
        )

        async for _ in _download_stream(transport, DLINK):
            pass

        assert "Range" not in respx.calls[0].request.headers
