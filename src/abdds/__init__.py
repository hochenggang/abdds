"""abdds - 百度网盘异步 Python SDK (async-baidu-disk-sdk)

简单、有效、报错清晰的百度网盘异步 SDK。

Usage::

    from abdds import AsyncBaiduPanClient

    async with AsyncBaiduPanClient(client_id, client_secret, app_name) as client:
        quota = await client.get_quota()
        await client.upload(Path("file.txt"), file_to=Path("/docs/file.txt"))
        await client.download(dlink, file_to=Path("output.txt"))
"""

__version__ = "0.1.0"

from .client import AsyncBaiduPanClient
from .errors import (
    BaiduPanAPIError,
    BaiduPanError,
    BaiduPanNetworkError,
    TokenExpiredError,
)
from .models import ApiFileListItem, ApiFileMeta, ApiQuotaInfo, UploadResult

__all__ = [
    # Client
    "AsyncBaiduPanClient",
    # Errors
    "BaiduPanError",
    "BaiduPanNetworkError",
    "BaiduPanAPIError",
    "TokenExpiredError",
    # Models
    "UploadResult",
    "ApiFileMeta",
    "ApiFileListItem",
    "ApiQuotaInfo",
]
