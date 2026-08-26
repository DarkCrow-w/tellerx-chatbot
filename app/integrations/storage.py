"""Content-addressed immutable storage for source files and vector artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import uuid
import zlib
from pathlib import Path
from typing import BinaryIO


def safe_filename(name: str) -> str:
    """移除目录与危险字符，生成仅用于展示的安全文件名。"""

    base = Path(name).name
    clean = re.sub(r"[^\w.()\-\u4e00-\u9fff ]+", "_", base, flags=re.UNICODE).strip()
    return clean[:240] or "document"


class LocalObjectStorage:
    """基于本地文件系统的不可变、内容寻址对象存储。"""

    def __init__(self, root: Path):
        """初始化并确保对象存储根目录存在。"""

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, stream: BinaryIO, filename: str, max_bytes: int) -> tuple[Path, str, int]:
        """流式保存上传文件，边读取边校验大小并计算内容哈希。"""

        name = safe_filename(filename)
        temp_path = self.root / f".{os.getpid()}-{uuid.uuid4().hex}-{name}.upload"
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
            target = target_dir / f"{sha}-{name}"
            try:
                # 硬链接提供“目标存在则不覆盖”的原子提交语义。
                os.link(temp_path, target)
            except FileExistsError:
                pass
            temp_path.unlink(missing_ok=True)
            return target, sha, size
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def resolve(self, storage_path: str) -> Path:
        """解析对象路径，并阻止相对路径逃逸存储根目录。"""

        supplied = Path(storage_path)
        path = (supplied if supplied.is_absolute() else self.root / supplied).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("Storage path escapes configured root")
        return path

    def save_bytes(self, relative_path: str, data: bytes) -> tuple[str, str, int]:
        """以不可覆盖语义保存字节；同路径不同内容视为对象碰撞。"""

        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        temp = target.with_name(f".{os.getpid()}-{uuid.uuid4().hex}-{target.name}.tmp")
        try:
            temp.write_bytes(data)
            try:
                os.link(temp, target)
            except FileExistsError:
                existing = target.read_bytes()
                if hashlib.sha256(existing).hexdigest() != digest:
                    raise ValueError(f"Immutable object collision at {relative_path}")
        finally:
            temp.unlink(missing_ok=True)
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
