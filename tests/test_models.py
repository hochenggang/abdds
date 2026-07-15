"""数据模型测试"""

from abdds.models import (
    ApiFileListItem,
    ApiFileMeta,
    _ApiPreUploadResponse,
    ApiQuotaInfo,
    UploadResult,
)


class TestUploadResult:
    def test_construction(self):
        r = UploadResult(fs_id=123, md5="abc", path="/apps/test/f.txt", size=1024)
        assert r.fs_id == 123
        assert r.md5 == "abc"
        assert r.path == "/apps/test/f.txt"
        assert r.size == 1024

    def test_equality(self):
        a = UploadResult(fs_id=1, md5="a", path="/a", size=10)
        b = UploadResult(fs_id=1, md5="a", path="/a", size=10)
        assert a == b


class TestApiQuotaInfo:
    def test_construction(self):
        q = ApiQuotaInfo(total=100, used=30)
        assert q.total == 100
        assert q.used == 30


class TestApiFileMeta:
    def test_construction(self):
        m = ApiFileMeta(md5="md5val", path="/apps/test/f.txt", size=50, fs_id=1, dlink="https://dlink", filename="f.txt")
        assert m.filename == "f.txt"
        assert m.dlink == "https://dlink"


class TestApiFileListItem:
    def test_construction(self):
        item = ApiFileListItem(md5="md5val", path="/apps/test/d", size=0, fs_id=2, is_dir=1, filename="d")
        assert item.is_dir == 1


class Test_ApiPreUploadResponse:
    def test_construction(self):
        r = _ApiPreUploadResponse(path="/a", uploadid="uid", block_list=[0], block_list_md5=["md5"], file_size=100)
        assert r.uploadid == "uid"
        assert r.block_list == [0]
