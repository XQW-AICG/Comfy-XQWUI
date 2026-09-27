"""base —— 统一存储访问接口。

本地文件系统存储的抽象层：materials / results / cache 三桶同一接口，
由 router 按桶解析实际根目录（支持自定义素材库 / 成品库路径）。
"""
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class StoredObject:
    """一次存取的标准化引用。"""
    key: str                       # 桶内相对路径（/ 分隔）
    bucket: str = ""
    backend: str = "local"         # 存储后端标识
    size: int = 0
    content_type: str = ""
    created_ms: int = 0
    meta: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"key": self.key, "bucket": self.bucket,
                "backend": self.backend, "size": self.size,
                "content_type": self.content_type,
                "created_ms": self.created_ms}


class StorageError(Exception):
    pass


class StorageBackend(ABC):
    """统一存储接口。key 一律使用「/」分隔的相对路径。"""

    name = "abstract"

    @abstractmethod
    def put(self, bucket: str, key: str, data: bytes,
            content_type: str = "") -> StoredObject:
        """写入（覆盖语义）。"""

    @abstractmethod
    def get(self, bucket: str, key: str) -> bytes:
        """读取；不存在抛 StorageError。"""

    @abstractmethod
    def delete(self, bucket: str, key: str) -> bool:
        """删除；返回是否确实删除了。"""

    @abstractmethod
    def exists(self, bucket: str, key: str) -> bool: ...

    @abstractmethod
    def list(self, bucket: str, prefix: str = "",
             limit: int = 0) -> list:
        """按前缀列出 StoredObject（按创建时间倒序）。"""

    @abstractmethod
    def open_path(self, bucket: str, key: str) -> str | None:
        """若内容是本地文件，返回绝对路径（供 Range 直读）；
        非本地后端返回 None（调用方走 download / 代理）。"""

    @abstractmethod
    def download_to(self, bucket: str, key: str, dst: str) -> str:
        """把对象拉到本地路径（结果合并 / 尾帧接力需要真实文件）。"""

    def upload_from(self, bucket: str, key: str, src_path: str,
                    content_type: str = "") -> StoredObject:
        """从本地文件上传（默认实现：读进内存再 put）。"""
        with open(src_path, "rb") as f:
            return self.put(bucket, key, f.read(), content_type)

    def health(self) -> dict:
        return {"backend": self.name, "ok": True}


def guess_content_type(key: str) -> str:
    import mimetypes
    return mimetypes.guess_type(os.path.basename(key))[0] \
        or "application/octet-stream"


def now_ms() -> int:
    import time
    return int(time.time() * 1000)
