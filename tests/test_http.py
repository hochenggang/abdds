"""HTTP 传输层异步测试"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from abdds._http import _HttpTransport, _truncate_data
from abdds.errors import BaiduPanAPIError, BaiduPanNetworkError, TokenExpiredError


class TestTruncateData:
    def test_short_string_unchanged(self):
        assert _truncate_data("hello") == "hello"

    def test_long_string_truncated(self):
        result = _truncate_data("x" * 1000)
        assert "truncated" in result
        assert len(result) < 1000

    def test_bytes_shown_as_binary(self):
        result = _truncate_data(b"\x00" * 100)
        assert "Binary Data" in result
        assert "100 bytes" in result

    def test_dict_values_truncated(self):
        result = _truncate_data({"key": "x" * 1000})
        assert "truncated" in result["key"]

    def test_list_items_truncated(self):
        result = _truncate_data(["x" * 1000])
        assert "truncated" in result[0]

    def test_nested_structure(self):
        data = {"a": [{"b": "x" * 1000}]}
        result = _truncate_data(data)
        assert "truncated" in result["a"][0]["b"]


class TestHttpTransportInit:
    def test_client_created(self):
        transport = _HttpTransport()
        assert transport.client is not None

    def test_custom_params(self):
        transport = _HttpTransport(max_retries=5, timeout=(10, 30))
        assert transport._max_retries == 5
        assert transport._timeout == (10, 30)


class TestHttpTransportRequest:
    @respx.mock
    async def test_successful_get(self):
        respx.get("https://example.com/api").mock(
            return_value=httpx.Response(200, json={"errno": 0, "data": "ok"})
        )

        transport = _HttpTransport(max_retries=0)
        transport.set_access_token("test_token")
        result = await transport.request("GET", "https://example.com/api")
        assert result["data"] == "ok"

    async def test_no_token_raises(self):
        transport = _HttpTransport(max_retries=0)
        with pytest.raises(TokenExpiredError):
            await transport.request("GET", "https://example.com/api")

    @respx.mock
    async def test_api_error_raised(self):
        respx.get("https://example.com/api").mock(
            return_value=httpx.Response(
                200,
                json={"errno": 31023, "errmsg": "file not found", "request_id": "req1"},
            )
        )

        transport = _HttpTransport(max_retries=0)
        transport.set_access_token("test_token")

        with pytest.raises(BaiduPanAPIError) as exc_info:
            await transport.request("GET", "https://example.com/api")

        assert exc_info.value.errno == 31023
        assert exc_info.value.errmsg == "file not found"
        assert exc_info.value.request_id == "req1"

    @respx.mock
    async def test_network_error_raised(self):
        respx.get("https://example.com/api").mock(
            side_effect=httpx.ConnectError("connection lost")
        )

        transport = _HttpTransport(max_retries=0)
        transport.set_access_token("test_token")

        with pytest.raises(BaiduPanNetworkError):
            await transport.request("GET", "https://example.com/api")

    @respx.mock
    async def test_token_expired_auto_refresh(self):
        # 第一次请求返回 token 过期
        route = respx.get("https://example.com/api")
        route.side_effect = [
            httpx.Response(200, json={"errno": -6, "errmsg": "token expired"}),
            httpx.Response(200, json={"errno": 0, "data": "refreshed"}),
        ]

        transport = _HttpTransport(max_retries=0)
        transport.set_access_token("old_token")

        # 设置异步 token 过期回调
        callback = AsyncMock(return_value="new_token")
        transport.set_token_expired_callback(callback)

        result = await transport.request("GET", "https://example.com/api")
        assert result["data"] == "refreshed"
        callback.assert_called_once()

    @respx.mock
    async def test_token_expired_callback_fails(self):
        respx.get("https://example.com/api").mock(
            return_value=httpx.Response(200, json={"errno": -6, "errmsg": "token expired"})
        )

        transport = _HttpTransport(max_retries=0)
        transport.set_access_token("old_token")

        # 异步回调返回 None（刷新失败）
        callback = AsyncMock(return_value=None)
        transport.set_token_expired_callback(callback)

        with pytest.raises(BaiduPanAPIError) as exc_info:
            await transport.request("GET", "https://example.com/api")
        assert exc_info.value.errno == -6

    @respx.mock
    async def test_invalid_json_response(self):
        respx.get("https://example.com/api").mock(
            return_value=httpx.Response(200, text="not json", headers={"content-type": "text/plain"})
        )

        transport = _HttpTransport(max_retries=0)
        transport.set_access_token("test_token")

        with pytest.raises(BaiduPanAPIError) as exc_info:
            await transport.request("GET", "https://example.com/api")
        assert exc_info.value.errno == -1


class TestHttpTransportClose:
    async def test_close(self):
        transport = _HttpTransport()
        # 先触发 client 初始化
        _ = transport.client
        await transport.close()
        assert transport._client is None
        assert transport._closed is True

    async def test_request_after_close_raises(self):
        """close() 后调用 request() 应抛 RuntimeError"""
        transport = _HttpTransport()
        transport.set_access_token("test_token")
        await transport.close()

        with pytest.raises(RuntimeError, match="Transport is closed"):
            await transport.request("GET", "https://example.com/api")
