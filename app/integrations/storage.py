"""Content-addressed immutable storage for source files and vector artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import struct
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)


class StoragePathTooLongError(OSError):
    """磁盘路径超出操作系统限制，需缩短存储目录。"""


@contextmanager
def storage_path_errors() -> Iterator[None]:
    """统一解释 Windows 206 和其他平台的文件名过长错误。"""

    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG or getattr(exc, "winerror", None) == 206:
            raise StoragePathTooLongError(
                "文件存储路径过长，无法保存或解析文档。请将 STORAGE_ROOT 设置为较短的绝对路径"
                "（例如 C:/tx-data），重新上传后重试；已有文件需迁移到新目录。"
            ) from exc
        raise


def _remove_temporary_file(path: Path) -> None:
    """临时文件清理失败应记录，但不能覆盖实际保存或解析错误。"""

    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("临时文件清理失败 error=%s errno=%s", type(exc).__name__, exc.errno)


def safe_filename(name: str) -> str:
    """移除目录与危险字符，生成仅用于展示的安全文件名。"""

    base = Path(name).name
    clean = re.sub(r"[^\w.()\-\u4e00-\u9fff ]+", "_", base, flags=re.UNICODE).strip()
    return clean[:240] or "document"


class LocalObjectStorage:
    """基于本地文件系统的不可变、内容寻址对象存储。"""

    @storage_path_errors()
    def __init__(self, root: Path):
        """初始化并确保对象存储根目录存在。"""

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @storage_path_errors()
    def save(self, stream: BinaryIO, filename: str, max_bytes: int) -> tuple[Path, str, int]:
        """流式保存上传文件，边读取边校验大小并计算内容哈希。"""

        # 展示名称由 documents 保留；磁盘名只使用内容哈希和解析所需的扩展名。
        suffix = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ""
        temp_path = self.root / f".{uuid.uuid4().hex}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("wb") as output:
                while data := stream.read(1024 * 1024):
                    size += len(data)
                    if size > max_bytes:
                        raise ValueError(f"File exceeds maximum upload size of {max_bytes} bytes")
                    digest.update(data)
                    output.write(data)
            sha = digest.hexdigest()
            target_dir = self.root / sha[:2] / sha[2:4]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{sha}{suffix}"
            try:
                # 硬链接提供“目标存在则不覆盖”的原子提交语义。
                os.link(temp_path, target)
            except FileExistsError:
                pass
            return target, sha, size
        finally:
            _remove_temporary_file(temp_path)

    @storage_path_errors()
    def resolve(self, storage_path: str) -> Path:
        """解析对象路径，并阻止相对路径逃逸存储根目录。"""

        supplied = Path(storage_path)
        path = (supplied if supplied.is_absolute() else self.root / supplied).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("Storage path escapes configured root")
        return path

    def delete(self, storage_path: str) -> bool:
        """删除一个受控对象，并顺带移除存储根目录以内的空父目录。"""

        path = self.resolve(storage_path)
        if not path.exists():
            return False
        if not path.is_file():
            raise ValueError("Storage object is not a file")
        path.unlink()
        root = self.root.resolve()
        parent = path.parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return True

    @storage_path_errors()
    def save_bytes(self, relative_path: str, data: bytes) -> tuple[str, str, int]:
        """以不可覆盖语义保存字节；同路径不同内容视为对象碰撞。"""

        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        temp = target.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            try:
                os.link(temp, target)
            except FileExistsError:
                existing = target.read_bytes()
                if hashlib.sha256(existing).hexdigest() != digest:
                    raise ValueError(f"Immutable object collision at {relative_path}")
        finally:
            _remove_temporary_file(temp)
        return str(target.relative_to(self.root.resolve())), digest, len(data)

    def save_json(self, relative_path: str, value: object) -> tuple[str, str, int]:
        """紧凑序列化并压缩 JSON 对象。"""

        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.save_bytes(relative_path, zlib.compress(data, level=6))

    def load_json(self, object_uri: str) -> object:
        """读取、解压并解析 JSON 对象。"""

        return json.loads(zlib.decompress(self.resolve(object_uri).read_bytes()))

    def save_vector(
        self, embedding_fingerprint: str, content_hash: str, vector: list[float]
    ) -> tuple[str, str, int]:
        """按小端 float32 序列化并压缩向量。"""

        payload = struct.pack(f"<{len(vector)}f", *vector)
        return self.save_bytes(
            f"embeddings/{embedding_fingerprint}/{content_hash}.f32.zlib",
            zlib.compress(payload, level=6),
        )

    def load_vector(self, object_uri: str, dimensions: int, checksum: str) -> list[float]:
        """校验校验和与维度后加载 float32 向量。"""

        compressed = self.resolve(object_uri).read_bytes()
        if hashlib.sha256(compressed).hexdigest() != checksum:
            raise ValueError(f"Embedding checksum mismatch for {object_uri}")
        payload = zlib.decompress(compressed)
        if len(payload) != dimensions * 4:
            raise ValueError(f"Embedding dimensions mismatch for {object_uri}")
        return list(struct.unpack(f"<{dimensions}f", payload))
