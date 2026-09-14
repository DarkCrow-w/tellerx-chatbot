"""S3 对象存储，条件写入保证不可变对象不被并发覆盖。"""

import hashlib
import logging
import re
import tempfile
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath

from app.integrations.storage import ImmutableObjectCollisionError, ObjectStorage

logger = logging.getLogger(__name__)


class StorageUnavailableError(RuntimeError):
    """对象服务不可达、拒绝访问或传输失败。"""


class ConditionalWriteConflict(RuntimeError):
    """S3 请求竞争，需要重新提交条件写入。"""


class S3ObjectStorage(ObjectStorage):
    def __init__(self, settings, client=None):
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket
        self.temp_dir = settings.storage_temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    @staticmethod
    def key(value):
        if (
            not value
            or "\\" in value
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or ":" in value
        ):
            raise ValueError("Invalid object key")
        return value

    def _call(self, operation, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        started = time.monotonic()
        try:
            return getattr(self.client, operation)(Bucket=self.bucket, **kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(kwargs.get("Key", self.bucket)) from exc
            if code in {"ConditionalRequestConflict", "409"}:
                raise ConditionalWriteConflict() from exc
            if code in {"PreconditionFailed", "412"}:
                raise FileExistsError(kwargs.get("Key")) from exc
            logger.warning("S3 operation=%s key=%s error=%s", operation, kwargs.get("Key"), code)
            raise StorageUnavailableError("Object storage request failed") from exc
        except BotoCoreError as exc:
            raise StorageUnavailableError("Object storage connection failed") from exc
        finally:
            logger.debug(
                "S3 operation=%s key=%s elapsed_ms=%.1f",
                operation,
                kwargs.get("Key"),
                (time.monotonic() - started) * 1000,
            )

    def available(self):
        try:
            self._call("head_bucket")
            return True
        except (StorageUnavailableError, FileNotFoundError):
            return False

    def list_objects(self, prefix):
        """分页列出指定知识库导入目录，绝不扫描整个 bucket。"""
        token = None
        while True:
            arguments = {"Prefix": prefix, "MaxKeys": 1000}
            if token:
                arguments["ContinuationToken"] = token
            page = self._call("list_objects_v2", **arguments)
            yield from page.get("Contents", [])
            if not page.get("IsTruncated"):
                return
            token = page["NextContinuationToken"]

    def open(self, key):
        return ObjectBody(self._call("get_object", Key=self.key(key))["Body"])

    def read_bytes(self, key):
        with self.open(key) as body:
            return body.read()

    def _put(self, key, stream, digest, size):
        key = self.key(key)
        try:
            self._conditional_put(key, stream, digest, size)
        except FileExistsError:
            with self.open(key) as body:
                actual = hashlib.file_digest(body, "sha256").hexdigest()
            if actual != digest:
                raise ImmutableObjectCollisionError(f"Immutable object collision at {key}")
        return key, digest, size

    def _conditional_put(self, key, stream, digest, size):
        for attempt in range(3):
            stream.seek(0)
            try:
                self._call(
                    "put_object",
                    Key=key,
                    Body=stream,
                    ContentLength=size,
                    IfNoneMatch="*",
                    Metadata={"sha256": digest},
                )
                return
            except ConditionalWriteConflict as exc:
                if attempt == 2:
                    raise StorageUnavailableError("Concurrent object write; retry request") from exc

    def close(self):
        self.client.close()

    def save_bytes(self, key, data):
        return self._put(key, BytesIO(data), hashlib.sha256(data).hexdigest(), len(data))

    def save(self, stream, filename, max_bytes):
        suffix = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ""
        digest, size = hashlib.sha256(), 0
        with tempfile.TemporaryFile(dir=self.temp_dir) as spool:
            while data := stream.read(1024 * 1024):
                size += len(data)
                if size > max_bytes:
                    raise ValueError(f"File exceeds maximum upload size of {max_bytes} bytes")
                digest.update(data)
                spool.write(data)
            sha = digest.hexdigest()
            spool.seek(0)
            return self._put(f"{sha[:2]}/{sha[2:4]}/{sha}{suffix}", spool, sha, size)

    @contextmanager
    def materialize(self, key):
        suffix = PurePosixPath(self.key(key)).suffix
        with tempfile.TemporaryDirectory(dir=self.temp_dir) as directory:
            path = Path(directory) / f"source{suffix}"
            with self.open(key) as body, path.open("wb") as output:
                while data := body.read(1024 * 1024):
                    output.write(data)
            yield path

    def delete(self, key):
        key = self.key(key)
        try:
            self._call("head_object", Key=key)
        except FileNotFoundError:
            return False
        self._call("delete_object", Key=key)
        return True


class ObjectBody:
    """Translate transfer failures, and own the HTTP body until explicitly closed."""

    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        from botocore.exceptions import BotoCoreError

        try:
            return self.body.read(size)
        except (BotoCoreError, OSError) as exc:
            raise StorageUnavailableError("Object storage transfer failed") from exc

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readable(self):
        return True

    def close(self):
        self.body.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
