"""local —— 工作台本地存储后端。

布局：root/{bucket}/{key...}；flat=True 时 root 即桶目录（自定义库路径）。
"""
import os

from shared.util import (atomic_copy_file, atomic_write_bytes,
                         now_ms)
from xqwui.config import storage_dir
from xqwui.storage.base import (
    StorageBackend, StoredObject, StorageError, guess_content_type)


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, root: str | None = None, flat: bool = False):
        self.root = os.path.realpath(root or storage_dir())
        self.flat = flat            # True：root 本身就是桶目录

    def _base(self, bucket: str) -> str:
        return self.root if self.flat else \
            os.path.join(self.root, bucket)

    def _full(self, bucket: str, key: str) -> str:
        if not bucket or not key:
            raise StorageError("bucket/key 不能为空")
        base = os.path.realpath(self._base(bucket))
        p = os.path.realpath(os.path.join(base, *key.split("/")))
        if not p.startswith(base + os.sep):
            raise StorageError("路径越界: %s/%s" % (bucket, key))
        return p

    def path_of(self, bucket: str, key: str) -> str:
        """对象本地文件路径（供流式读取，避免整文件进内存）。"""
        return self._full(bucket, key)

    def put(self, bucket, key, data, content_type=""):
        p = self._full(bucket, key)
        atomic_write_bytes(p, data)
        return StoredObject(key=key, bucket=bucket, backend=self.name,
                            size=len(data),
                            content_type=content_type
                            or guess_content_type(key),
                            created_ms=now_ms())

    def get(self, bucket, key):
        p = self._full(bucket, key)
        if not os.path.isfile(p):
            raise StorageError("对象不存在: %s/%s" % (bucket, key))
        with open(p, "rb") as f:
            return f.read()

    def delete(self, bucket, key):
        p = self._full(bucket, key)
        if os.path.isfile(p):
            os.remove(p)
            return True
        return False

    def exists(self, bucket, key):
        return os.path.isfile(self._full(bucket, key))

    def list(self, bucket: str, prefix: str = "", limit: int = 0):
        base = os.path.realpath(self._base(bucket))
        out = []
        if not os.path.isdir(base):
            return out
        pre = prefix.strip("/")
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn.endswith(".tmp"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), base)
                rel = rel.replace(os.sep, "/")
                if pre and not rel.startswith(pre):
                    continue
                full = os.path.join(root, fn)
                st = os.stat(full)
                out.append(StoredObject(
                    key=rel, bucket=bucket, backend=self.name,
                    size=st.st_size,
                    content_type=guess_content_type(rel),
                    created_ms=int(st.st_mtime * 1000)))
        out.sort(key=lambda o: -o.created_ms)
        return out[:limit] if limit else out

    def open_path(self, bucket, key):
        p = self._full(bucket, key)
        return p if os.path.isfile(p) else None

    def download_to(self, bucket, key, dst):
        p = self._full(bucket, key)
        if not os.path.isfile(p):
            raise StorageError("对象不存在: %s/%s" % (bucket, key))
        os.makedirs(os.path.dirname(os.path.realpath(dst)) or ".",
                    exist_ok=True)
        if os.path.islink(dst) or os.path.realpath(p) != os.path.realpath(dst):
            import shutil
            shutil.copyfile(p, dst)
        return dst

    def upload_from(self, bucket, key, src_path, content_type=""):
        # 本地同盘：分块原子拷贝，避免整读内存
        p = self._full(bucket, key)
        src = os.path.realpath(src_path)
        st = os.stat(src)
        atomic_copy_file(src, p)
        return StoredObject(key=key, bucket=bucket, backend=self.name,
                            size=st.st_size,
                            content_type=content_type or guess_content_type(key),
                            created_ms=now_ms())
