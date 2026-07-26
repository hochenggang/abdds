"""HTTP 传输层 - 异步 Session 管理、请求封装、重试策略"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .errors import BaiduPanAPIError, BaiduPanNetworkError, TokenExpiredError

logger = logging.getLogger("abdds")

_USER_AGENT = "netdisk;2.2.51.6;netdisk;10.0;PC;PC;Mac OS X 10.15.7;en-US"


def _truncate_data(data: Any, max_len: int = 500) -> Any:
    """递归截断超长字符串和二进制数据，用于日志安全输出"""
    if isinstance(data, dict):
        return {k: _truncate_data(v, max_len) for k, v in data.items()}
    if isinstance(data, (list, tuple, set)):
        return [_truncate_data(i, max_len) for i in data]
    if isinstance(data, (bytes, bytearray)):
        return f"<Binary Data: {len(data)} bytes>"
    if isinstance(data, str) and len(data) > max_len:
        return f"{data[:max_len]}... [truncated]"
    return data


class _HttpTransport:
    """封装 httpx.AsyncClient，提供自动 Token 注入、重试和错误处理"""

    def __init__(
        self,
        max_retries: int = 3,
        timeout: tuple[int, int] = (100, 600),
    ) -> None:
        self._max_retries = max_retries
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._closed: bool = False
        self._access_token: str | None = None
        self._on_token_expired: Callable[[], Awaitable[str | None]] | None = None

    def _init_client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(
            retries=self._max_retries,
        )
        client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self._timeout[1], connect=self._timeout[0]),
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
        return client

    @property
    def client(self) -> httpx.AsyncClient:
        """获取或初始化 AsyncClient"""
        if self._closed:
            raise RuntimeError("Transport is closed, cannot make requests")
        if self._client is None:
            self._client = self._init_client()
        return self._client

    def set_access_token(self, token: str | None) -> None:
        self._access_token = token

    def set_token_expired_callback(
        self, callback: Callable[[], Awaitable[str | None]]
    ) -> None:
        """设置 Token 过期时的异步刷新回调，回调应返回新的 access_token"""
        self._on_token_expired = callback

    async def request(
        self, method: str, url: str, **kwargs: Any
    ) -> dict | httpx.Response:
        """
        核心异步请求方法：
        1. 自动注入 access_token
        2. 处理网络层错误 (由 httpx transport retries 处理重试)
        3. 处理业务层错误 (解析 errno)
        4. Token 失效时自动刷新并重试一次
        """
        logger.debug("method:%s url:%s kwargs:%s", method, url, _truncate_data(kwargs))

        if self._closed:
            raise RuntimeError("Transport is closed, cannot make requests")

        if not self._access_token:
            raise TokenExpiredError("Access token not initialized, please login first")

        params = kwargs.pop("params", {})
        params["access_token"] = self._access_token

        try:
            response = await self.client.request(method, url, params=params, **kwargs)
            response.raise_for_status()

            # 流式响应直接返回
            if kwargs.get("stream"):
                return response  # type: ignore[return-value]

            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError):
                raise BaiduPanAPIError(-1, "Invalid JSON response")
            logger.debug("method:%s url:%s response:%s", method, url, _truncate_data(data))

        except httpx.HTTPStatusError as e:
            raise BaiduPanNetworkError(
                f"HTTP {e.response.status_code}: {e}", url=url,
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise BaiduPanNetworkError(
                f"Request failed: {e}", url=url
            ) from e

        # 业务错误检查
        errno = data.get("errno", 0)
        if errno != 0:
            # Token 失效 (errno: -6 或 11)
            if errno in (-6, 11) and self._on_token_expired:
                logger.warning("Token expired during request, refreshing...")
                new_token = await self._on_token_expired()
                if new_token:
                    params["access_token"] = new_token
                    try:
                        response = await self.client.request(
                            method, url, params=params, **kwargs
                        )
                        response.raise_for_status()
                        data = response.json()
                    except httpx.RequestError as e:
                        raise BaiduPanNetworkError(
                            f"Retry after token refresh failed: {e}", url=url
                        ) from e
                    except (json.JSONDecodeError, ValueError):
                        raise BaiduPanAPIError(-1, "Invalid JSON response")

                    if data.get("errno", 0) == 0:
                        return data

                # 刷新后仍失败
                raise BaiduPanAPIError(
                    data.get("errno", errno),
                    data.get("errmsg", "Token refresh failed"),
                    request_id=data.get("request_id", ""),
                )

            raise BaiduPanAPIError(
                errno,
                data.get("errmsg", "Unknown error"),
                request_id=data.get("request_id", ""),
            )

        return data

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
