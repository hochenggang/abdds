"""AsyncBaiduPanClient - 百度网盘异步 Python SDK 主类"""

from __future__ import annotations

import json
import logging
import stat
import time
from pathlib import Path, PurePosixPath
from typing import AsyncGenerator, Awaitable, Callable
from urllib.parse import quote

import aiofiles
import httpx

from ._http import _HttpTransport
from .download import download as _download
from .errors import BaiduPanNetworkError, TokenExpiredError
from .models import ApiFileListItem, ApiFileMeta, ApiQuotaInfo, UploadResult
from .upload import upload as _upload
from .upload import UploadSource, _is_async_generator, _is_generator

logger = logging.getLogger("abdds")


class AsyncBaiduPanClient:
    """
    百度网盘异步 Python SDK 客户端

    具备自动重试、Token 管理、流式下载和分片上传。

    Usage::

        async with AsyncBaiduPanClient(client_id, client_secret, app_name) as client:
            if not client.access_token:
                print(client.auth_url)
                code = input("Enter code: ")
                await client.fetch_token(code)

            quota = await client.get_quota()
            result = await client.upload(Path("local_file.txt"))
    """

    HOST_OAUTH = "https://openapi.baidu.com/oauth/2.0"
    HOST_PAN = "https://pan.baidu.com"

    URL_QUOTA = f"{HOST_PAN}/api/quota"
    URL_FILE = f"{HOST_PAN}/rest/2.0/xpan/file"
    URL_MULTIMEDIA = f"{HOST_PAN}/rest/2.0/xpan/multimedia"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        app_name: str,
        config_dir: Path | None = None,
        max_retries: int = 3,
        timeout: tuple[int, int] | None = None,
    ) -> None:
        """
        初始化百度网盘异步客户端

        Args:
            client_id: 百度开放平台应用的 API Key
            client_secret: 百度开放平台应用的 Secret Key
            app_name: 应用名称
            config_dir: Token 存储目录，默认 ~/.baidupan
            max_retries: 请求重试次数，默认 3
            timeout: 请求超时 (connect, read)，默认 (100, 600)
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self.app_name = app_name
        self.pan_dir_base = f"/apps/{app_name}"

        if config_dir is None:
            config_dir = Path.home() / ".baidupan"
        self.config_dir = config_dir
        self._token_file = self.config_dir / f"access_token_{client_id}.json"

        self._token_data: dict = {}
        self._access_token: str | None = None
        self._refresh_token: str | None = None

        # 初始化异步 HTTP 传输层
        self._transport = _HttpTransport(
            max_retries=max_retries,
            timeout=timeout or (100, 600),
        )
        self._transport.set_token_expired_callback(self._handle_token_expired)

        # 注意: _load_token 是异步方法，不能在 __init__ 中直接调用
        # 需要用户在初始化后手动调用 await client._load_token()
        # 或者使用 create 类方法

    @classmethod
    async def create(
        cls,
        client_id: str,
        client_secret: str,
        app_name: str,
        config_dir: Path | None = None,
        max_retries: int = 3,
        timeout: tuple[int, int] | None = None,
    ) -> AsyncBaiduPanClient:
        """异步工厂方法，初始化客户端并自动加载已保存的 Token"""
        instance = cls(client_id, client_secret, app_name, config_dir, max_retries, timeout)
        await instance._load_token()
        return instance

    # ---- 上下文管理器 ----

    async def close(self) -> None:
        """关闭客户端，释放资源"""
        await self._transport.close()

    async def __aenter__(self) -> AsyncBaiduPanClient:
        await self._load_token()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool | None:
        await self.close()
        return False

    # ---- Token 管理 ----

    @property
    def access_token(self) -> str | None:
        """当前 access_token（只读）"""
        return self._access_token

    @property
    def auth_url(self) -> str:
        """生成用户授权 URL"""
        return (
            f"{self.HOST_OAUTH}/authorize?"
            f"response_type=code&client_id={self._client_id}&redirect_uri=oob&scope=basic,netdisk"
        )

    async def fetch_token(self, code: str) -> None:
        """
        通过授权码获取 Access Token

        Args:
            code: 用户授权后获得的授权码
        """
        url = f"{self.HOST_OAUTH}/token"
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": "oob",
        }
        try:
            resp = await self._transport.client.get(url, params=params, timeout=10)
            resp.raise_for_status()
            result = resp.json()
        except httpx.RequestError as e:
            raise BaiduPanNetworkError(f"Fetch token failed: {e}") from e

        # 检查 API 层面错误
        if "error" in result:
            raise TokenExpiredError(
                f"Fetch token failed: {result.get('error_description', result['error'])}"
            )
        await self._save_token(result)

    async def refresh_token(self) -> str:
        """
        刷新 Access Token

        Returns:
            新的 access_token

        Raises:
            TokenExpiredError: 无 refresh_token 可用
        """
        if not self._refresh_token:
            raise TokenExpiredError("No refresh token available, please re-login")

        url = f"{self.HOST_OAUTH}/token"
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            resp = await self._transport.client.get(url, params=params, timeout=10)
            resp.raise_for_status()
            result = resp.json()
        except httpx.RequestError as e:
            raise BaiduPanNetworkError(f"Refresh token failed: {e}") from e

        # 检查 API 层面错误
        if "error" in result:
            raise TokenExpiredError(
                f"Refresh token failed: {result.get('error_description', result['error'])}"
            )
        await self._save_token(result)
        return self._access_token  # type: ignore[return-value]

    async def _handle_token_expired(self) -> str | None:
        """Token 过期回调，供 _HttpTransport 调用"""
        try:
            return await self.refresh_token()
        except (TokenExpiredError, BaiduPanNetworkError):
            logger.error("Token refresh failed in auto-retry")
            return None

    async def _load_token(self) -> None:
        """从本地文件异步加载 Token"""
        if not self._token_file.exists():
            return
        try:
            async with aiofiles.open(self._token_file, encoding="utf-8") as f:
                content = await f.read()
            data = json.loads(content)
            self._token_data = data
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._transport.set_access_token(self._access_token)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Failed to load token: %s", e)

    async def _save_token(self, data: dict) -> None:
        """异步持久化保存 Token"""
        if "access_token" not in data:
            error_desc = data.get("error_description", data.get("Error", "Unknown error"))
            raise TokenExpiredError(f"Token response missing access_token: {error_desc}")

        self.config_dir.mkdir(parents=True, exist_ok=True)
        data["update_at"] = int(time.time())
        self._token_data = data
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        self._transport.set_access_token(self._access_token)

        async with aiofiles.open(self._token_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=2))
        # 限制文件权限为仅拥有者可读写
        try:
            self._token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows 不完全支持 chmod

        logger.info("Access token saved")

    # ---- 业务 API ----

    async def get_quota(self) -> ApiQuotaInfo:
        """
        获取网盘空间配额

        https://pan.baidu.com/union/doc/Cksg0s9ic
        """
        data = await self._transport.request("GET", self.URL_QUOTA)
        return ApiQuotaInfo(total=data.get("total", 0), used=data.get("used", 0))

    async def get_file_metas(self, fsids: list[int]) -> list[ApiFileMeta]:
        """
        获取文件元信息

        https://pan.baidu.com/union/doc/Fksg0sbcm

        Args:
            fsids: 文件 ID 列表
        """
        if not fsids:
            return []

        params = {"method": "filemetas", "fsids": json.dumps(fsids), "dlink": 1}
        data = await self._transport.request("GET", self.URL_MULTIMEDIA, params=params)
        items = data.get("list", [])

        return [
            ApiFileMeta(
                fs_id=item["fs_id"],
                filename=item["filename"],
                path=item["path"],
                size=item["size"],
                md5=item.get("md5", ""),
                dlink=item.get("dlink", ""),
            )
            for item in items
        ]

    async def get_file_list(self, path: str) -> list[ApiFileListItem]:
        """
        获取指定目录下的文件列表

        https://pan.baidu.com/union/doc/nksg0sat9

        Args:
            path: 目录路径
        """
        params = {"method": "list", "dir": quote(path)}
        data = await self._transport.request("GET", self.URL_FILE, params=params)
        items = data.get("list", [])

        return [
            ApiFileListItem(
                fs_id=item["fs_id"],
                filename=item["server_filename"],
                path=item["path"],
                size=item["size"],
                md5=item.get("md5", ""),
                is_dir=item.get("isdir", 0),
            )
            for item in items
        ]

    async def delete_files(self, file_paths: list[str]) -> None:
        """
        批量删除文件

        https://pan.baidu.com/union/doc/mksg0s9l4

        Args:
            file_paths: 要删除的文件路径列表
        """
        if not file_paths:
            return
        params = {"method": "filemanager", "opera": "delete"}
        data = {"async": 2, "filelist": json.dumps(file_paths)}
        await self._transport.request("POST", self.URL_FILE, params=params, data=data)
        logger.info("Deleted %d file(s)", len(file_paths))

    # ---- 上传 ----

    async def upload(
        self,
        file_from: UploadSource,
        file_to: Path | None = None,
    ) -> UploadResult:
        """
        异步上传数据到百度网盘

        - 小文件 (<=4MB): 单步上传
        - 大文件 (>4MB): 分片上传

        Args:
            file_from: 数据来源，支持 Path / bytes / Generator[bytes] / AsyncGenerator[bytes]
            file_to: 远程路径 (Path)，拼接在 /apps/{app_name} 之后。
                默认使用 file_from 的文件名 (Path 时) 或 "upload"

        Returns:
            UploadResult: 上传结果

        Raises:
            TypeError: file_from 或 file_to 类型不正确
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
        if file_to is not None and not isinstance(file_to, Path):
            raise TypeError(
                f"file_to must be Path or None, got {type(file_to).__name__}"
            )

        if isinstance(file_from, Path):
            default_name = file_from.name
        else:
            default_name = "upload"

        if file_to is None:
            remote_path = PurePosixPath(f"/{default_name}")
        else:
            # 将 Path 转为 PurePosixPath 以确保 POSIX 风格
            remote_path = PurePosixPath(file_to.as_posix())
            if not remote_path.is_absolute():
                remote_path = PurePosixPath(f"/{remote_path}")

        return await _upload(self._transport, file_from, remote_path, self.pan_dir_base)

    # ---- 下载 ----

    async def download(
        self,
        dlink: str,
        file_to: Path | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncGenerator[bytes, None] | Path:
        """
        异步下载文件

        - file_to=None: 返回异步字节迭代器 (流式下载)
        - file_to=Path: 下载到本地文件，返回 Path

        https://pan.baidu.com/union/doc/pkuo3snyp

        Args:
            dlink: 下载链接 (通过 get_file_metas 获取)
            file_to: 本地保存路径 (Path)，为 None 时返回迭代器
            chunk_size: 分块大小，默认 1MB

        Returns:
            AsyncGenerator[bytes] 当 file_to=None，Path 当 file_to 指定路径

        Raises:
            TypeError: file_to 类型不正确
        """
        if file_to is not None and not isinstance(file_to, Path):
            raise TypeError(
                f"file_to must be Path or None, got {type(file_to).__name__}"
            )
        return await _download(self._transport, dlink, file_to, chunk_size)
