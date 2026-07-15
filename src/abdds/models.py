"""abdds 数据模型"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UploadResult:
    """上传结果"""

    fs_id: int | None
    md5: str
    path: str
    size: int | None


@dataclass
class _ApiPreUploadResponse:
    """预上传响应（内部模型）"""

    path: str
    uploadid: str
    block_list: list[int]
    block_list_md5: list[str]
    file_size: int


@dataclass
class ApiFileMeta:
    """文件元信息"""

    md5: str
    path: str
    size: int
    fs_id: int
    dlink: str
    filename: str


@dataclass
class ApiFileListItem:
    """文件列表项"""

    md5: str
    path: str
    size: int
    fs_id: int
    is_dir: int
    filename: str


@dataclass
class ApiQuotaInfo:
    """空间配额信息"""

    total: int
    used: int
