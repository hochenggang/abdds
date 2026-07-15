"""测试公共 fixtures"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abdds import AsyncBaiduPanClient
from abdds._http import _HttpTransport


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """隔离的配置目录，避免污染真实 token"""
    d = tmp_path / ".baidupan"
    d.mkdir()
    return d


@pytest.fixture
def mock_transport() -> _HttpTransport:
    """预置 access_token 的 _HttpTransport"""
    transport = _HttpTransport(max_retries=0, timeout=(5, 10))
    transport.set_access_token("test_access_token")
    return transport


@pytest.fixture
def mock_client(tmp_config_dir: Path) -> AsyncBaiduPanClient:
    """预置 access_token 的 AsyncBaiduPanClient（跳过真实网络）"""
    client = AsyncBaiduPanClient(
        client_id="test_id",
        client_secret="test_secret",
        app_name="test_app",
        config_dir=tmp_config_dir,
    )
    # 手动注入 token
    client._access_token = "test_access_token"
    client._refresh_token = "test_refresh_token"
    client._token_data = {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "expires_in": 2592000,
        "update_at": 9999999999,
    }
    client._transport.set_access_token("test_access_token")
    return client
