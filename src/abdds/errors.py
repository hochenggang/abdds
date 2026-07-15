"""abdds 异常体系"""


class BaiduPanError(Exception):
    """SDK 基础异常"""


class BaiduPanNetworkError(BaiduPanError):
    """网络通信异常"""

    def __init__(
        self, message: str, *, url: str = "", status_code: int | None = None
    ):
        self.url = url
        self.status_code = status_code
        super().__init__(message)


class BaiduPanAPIError(BaiduPanError):
    """API 业务异常 (errno != 0)"""

    def __init__(
        self, errno: int, errmsg: str = "", *, request_id: str = ""
    ):
        self.errno = errno
        self.errmsg = errmsg
        self.request_id = request_id
        detail = f"[errno:{errno}] {errmsg}"
        if request_id:
            detail += f" (request_id:{request_id})"
        super().__init__(detail)


class TokenExpiredError(BaiduPanError):
    """Token 过期或无效"""

    def __init__(self, message: str = "Access token expired or invalid"):
        super().__init__(message)
