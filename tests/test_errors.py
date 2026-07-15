"""异常体系测试"""

from abdds.errors import (
    BaiduPanAPIError,
    BaiduPanError,
    BaiduPanNetworkError,
    TokenExpiredError,
)


class TestExceptionHierarchy:
    """异常继承关系"""

    def test_base_is_exception(self):
        assert issubclass(BaiduPanError, Exception)

    def test_network_inherits_base(self):
        assert issubclass(BaiduPanNetworkError, BaiduPanError)

    def test_api_inherits_base(self):
        assert issubclass(BaiduPanAPIError, BaiduPanError)

    def test_token_expired_inherits_base(self):
        assert issubclass(TokenExpiredError, BaiduPanError)


class TestBaiduPanAPIError:
    def test_attributes(self):
        err = BaiduPanAPIError(errno=31023, errmsg="file does not exist", request_id="req_123")
        assert err.errno == 31023
        assert err.errmsg == "file does not exist"
        assert err.request_id == "req_123"

    def test_str_with_request_id(self):
        err = BaiduPanAPIError(errno=31023, errmsg="file does not exist", request_id="req_123")
        assert "[errno:31023]" in str(err)
        assert "file does not exist" in str(err)
        assert "req_123" in str(err)

    def test_str_without_request_id(self):
        err = BaiduPanAPIError(errno=31023, errmsg="file does not exist")
        assert "[errno:31023]" in str(err)
        assert "request_id" not in str(err)

    def test_default_errmsg(self):
        err = BaiduPanAPIError(errno=-1)
        assert str(err) == "[errno:-1] "


class TestBaiduPanNetworkError:
    def test_attributes(self):
        err = BaiduPanNetworkError("Connection failed", url="https://example.com", status_code=503)
        assert err.url == "https://example.com"
        assert err.status_code == 503
        assert "Connection failed" in str(err)

    def test_defaults(self):
        err = BaiduPanNetworkError("timeout")
        assert err.url == ""
        assert err.status_code is None


class TestTokenExpiredError:
    def test_default_message(self):
        err = TokenExpiredError()
        assert "expired" in str(err).lower() or "invalid" in str(err).lower()

    def test_custom_message(self):
        err = TokenExpiredError("custom msg")
        assert str(err) == "custom msg"
