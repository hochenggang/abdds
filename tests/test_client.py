"""AsyncBaiduPanClient 测试"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from abdds import AsyncBaiduPanClient, BaiduPanNetworkError, TokenExpiredError
from abdds.models import ApiFileListItem, ApiFileMeta, ApiQuotaInfo


class TestClientInit:
    def test_basic_init(self, tmp_config_dir: Path):
        client = AsyncBaiduPanClient("id", "secret", "myapp", config_dir=tmp_config_dir)
        assert client._app_dir == "/apps/myapp"
        assert client.access_token is None
        assert not client.is_authenticated

    async def test_context_manager(self, tmp_config_dir: Path):
        async with AsyncBaiduPanClient("id", "secret", "myapp", config_dir=tmp_config_dir) as client:
            assert client is not None
        # 关闭后 client 应该被关闭
        assert client._transport._client is None

    def test_auth_url(self, tmp_config_dir: Path):
        client = AsyncBaiduPanClient("my_client_id", "secret", "app", config_dir=tmp_config_dir)
        url = client.auth_url
        assert "my_client_id" in url
        assert "authorize" in url

    def test_repr(self, tmp_config_dir: Path):
        client = AsyncBaiduPanClient("id", "secret", "myapp", config_dir=tmp_config_dir)
        assert "myapp" in repr(client)
        assert "not authenticated" in repr(client)

        client._access_token = "token"
        assert "authenticated" in repr(client)


class TestTokenManagement:
    @respx.mock
    async def test_fetch_token(self, tmp_config_dir: Path):
        respx.get("https://openapi.baidu.com/oauth/2.0/token").mock(
            return_value=httpx.Response(200, json={
                "access_token": "new_access",
                "refresh_token": "new_refresh",
                "expires_in": 2592000,
            })
        )

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        await client.fetch_token("auth_code_123")

        assert client.access_token == "new_access"
        assert client.is_authenticated
        assert client._refresh_token == "new_refresh"

        # token 文件应已创建
        token_file = tmp_config_dir / "access_token_id.json"
        assert token_file.exists()
        saved = json.loads(token_file.read_text())
        assert saved["access_token"] == "new_access"

    @respx.mock
    async def test_refresh_token(self, tmp_config_dir: Path):
        respx.get("https://openapi.baidu.com/oauth/2.0/token").mock(
            return_value=httpx.Response(200, json={
                "access_token": "refreshed_access",
                "refresh_token": "refreshed_refresh",
                "expires_in": 2592000,
            })
        )

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        client._refresh_token = "old_refresh"

        result = await client.refresh_token()
        assert result == "refreshed_access"
        assert client.access_token == "refreshed_access"

    async def test_refresh_token_without_refresh_token(self, tmp_config_dir: Path):
        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        with pytest.raises(TokenExpiredError):
            await client.refresh_token()

    @respx.mock
    async def test_fetch_token_network_error(self, tmp_config_dir: Path):
        respx.get("https://openapi.baidu.com/oauth/2.0/token").mock(
            side_effect=httpx.ConnectError("failed")
        )

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        with pytest.raises(BaiduPanNetworkError):
            await client.fetch_token("code")

    async def test_load_token(self, tmp_config_dir: Path):
        token_file = tmp_config_dir / "access_token_id.json"
        token_file.write_text(json.dumps({
            "access_token": "loaded_access",
            "refresh_token": "loaded_refresh",
        }))

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        await client._load_token()
        assert client.access_token == "loaded_access"
        assert client.is_authenticated


class TestBusinessAPI:
    @respx.mock
    async def test_quota(self, mock_client: AsyncBaiduPanClient):
        respx.get("https://pan.baidu.com/api/quota").mock(
            return_value=httpx.Response(200, json={"errno": 0, "total": 214748364800, "used": 1073741824})
        )

        quota = await mock_client.quota()
        assert isinstance(quota, ApiQuotaInfo)
        assert quota.total == 214748364800
        assert quota.used == 1073741824

    @respx.mock
    async def test_file_metas(self, mock_client: AsyncBaiduPanClient):
        respx.get("https://pan.baidu.com/rest/2.0/xpan/multimedia").mock(
            return_value=httpx.Response(200, json={
                "errno": 0,
                "list": [
                    {
                        "fs_id": 123,
                        "filename": "test.txt",
                        "path": "/apps/test/test.txt",
                        "size": 1024,
                        "md5": "abc123",
                        "dlink": "https://dlink.example.com/f",
                    }
                ],
            })
        )

        metas = await mock_client.file_metas([123])
        assert len(metas) == 1
        assert isinstance(metas[0], ApiFileMeta)
        assert metas[0].fs_id == 123
        assert metas[0].dlink == "https://dlink.example.com/f"

    async def test_file_metas_empty_fsids(self, mock_client: AsyncBaiduPanClient):
        result = await mock_client.file_metas([])
        assert result == []

    @respx.mock
    async def test_list_files(self, mock_client: AsyncBaiduPanClient):
        respx.get("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={
                "errno": 0,
                "list": [
                    {
                        "fs_id": 1,
                        "server_filename": "dir1",
                        "path": "/apps/test/dir1",
                        "size": 0,
                        "md5": "",
                        "isdir": 1,
                    },
                    {
                        "fs_id": 2,
                        "server_filename": "file.txt",
                        "path": "/apps/test/file.txt",
                        "size": 50,
                        "md5": "md5val",
                        "isdir": 0,
                    },
                ],
            })
        )

        items = await mock_client.list_files("/apps/test/")
        assert len(items) == 2
        assert isinstance(items[0], ApiFileListItem)
        assert items[0].is_dir == 1
        assert items[1].filename == "file.txt"

    @respx.mock
    async def test_delete(self, mock_client: AsyncBaiduPanClient):
        route = respx.post("https://pan.baidu.com/rest/2.0/xpan/file").mock(
            return_value=httpx.Response(200, json={"errno": 0})
        )

        await mock_client.delete(["/apps/test/f.txt"])
        assert route.called

    async def test_delete_empty_list(self, mock_client: AsyncBaiduPanClient):
        # 空列表不应发起请求
        await mock_client.delete([])

    @respx.mock
    async def test_no_token_raises(self, tmp_config_dir: Path):
        respx.get("https://pan.baidu.com/api/quota").mock(
            return_value=httpx.Response(200, json={"errno": 0, "total": 100, "used": 0})
        )

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        with pytest.raises(TokenExpiredError):
            await client.quota()


class TestTokenEdgeCases:
    @respx.mock
    async def test_fetch_token_api_error(self, tmp_config_dir: Path):
        """API 返回 error 字段时应抛 TokenExpiredError"""
        respx.get("https://openapi.baidu.com/oauth/2.0/token").mock(
            return_value=httpx.Response(200, json={"error": "invalid_code", "error_description": "Authorization code is invalid"})
        )

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        with pytest.raises(TokenExpiredError, match="Authorization code is invalid"):
            await client.fetch_token("bad_code")

    @respx.mock
    async def test_refresh_token_api_error(self, tmp_config_dir: Path):
        """刷新 token 时 API 返回错误"""
        respx.get("https://openapi.baidu.com/oauth/2.0/token").mock(
            return_value=httpx.Response(200, json={"error": "invalid_refresh_token", "error_description": "Refresh token has expired"})
        )

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        client._refresh_token = "old_refresh"
        with pytest.raises(TokenExpiredError, match="Refresh token has expired"):
            await client.refresh_token()

    @respx.mock
    async def test_save_token_missing_access_token(self, tmp_config_dir: Path):
        """_save_token 收到不含 access_token 的数据应报错"""
        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        with pytest.raises(TokenExpiredError, match="missing access_token"):
            await client._save_token({"refresh_token": "rt"})

    async def test_load_token_corrupted_file(self, tmp_config_dir: Path):
        """token 文件损坏时应优雅降级"""
        token_file = tmp_config_dir / "access_token_id.json"
        token_file.write_text("NOT VALID JSON{{{")

        client = AsyncBaiduPanClient("id", "secret", "app", config_dir=tmp_config_dir)
        await client._load_token()
        assert client.access_token is None


class TestUploadDownloadEdgeCases:
    async def test_upload_source_type_error(self, mock_client: AsyncBaiduPanClient):
        """source 传入 str 应抛 TypeError"""
        with pytest.raises(TypeError, match="source must be"):
            await mock_client.upload("not_a_path")  # type: ignore

    async def test_download_to_file_type_error(self, mock_client: AsyncBaiduPanClient):
        """download_to_file local_path 传入 str 应抛 TypeError"""
        with pytest.raises(TypeError, match="local_path must be Path"):
            await mock_client.download_to_file("https://dlink", "/str_path")  # type: ignore
