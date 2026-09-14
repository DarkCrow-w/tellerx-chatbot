"""Shared storage contract and offline migration integrity tests."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Lock

import pytest
from botocore.exceptions import ClientError

from app.commands.storage_migrate import transfer
from app.core.config import Settings
from app.integrations.s3_storage import S3ObjectStorage, StorageUnavailableError
from app.integrations.storage import LocalObjectStorage


class MemoryS3:
    def __init__(self):
        self.data = {}
        self.lock = Lock()

    def put_object(self, **args):
        assert args["IfNoneMatch"] == "*"
        with self.lock:
            if args["Key"] in self.data:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.data[args["Key"]] = args["Body"].read()

    def get_object(self, **args):
        if args["Key"] not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.data[args["Key"]])}

    def head_object(self, **args):
        self.get_object(**args)

    def delete_object(self, **args):
        self.data.pop(args["Key"], None)


@pytest.fixture(params=["local", "s3"])
def storage(request, tmp_path):
    if request.param == "local":
        return LocalObjectStorage(tmp_path)
    return S3ObjectStorage(Settings(_env_file=None, storage_temp_dir=tmp_path), MemoryS3())


def test_shared_contract(storage):
    key, sha, size = storage.save(BytesIO(b"hello"), "中文.PDF", 100)
    assert key.endswith(".pdf") and size == 5
    assert storage.read_bytes(key) == b"hello"
    with storage.materialize(key) as path:
        assert path.read_bytes() == b"hello"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: storage.save(BytesIO(b"hello"), "中文.PDF", 100), range(8))
        )
    assert all(result == (key, sha, size) for result in results)
    with pytest.raises(ValueError):
        storage.save_bytes(key, b"collision")
    assert storage.read_bytes(key) == b"hello"
    assert storage.delete(key)
    assert not storage.delete(key)


def test_codecs_limits_and_cleanup(storage):
    uri, _, _ = storage.save_json("parsed/x.json.zlib", {"中文": "内容"})
    assert storage.load_json(uri) == {"中文": "内容"}
    uri, checksum, _ = storage.save_vector("model", "hash", [1.25, -2.5])
    assert storage.load_vector(uri, 2, checksum) == [1.25, -2.5]
    with pytest.raises(ValueError):
        storage.load_vector(uri, 3, checksum)
    with pytest.raises(ValueError):
        storage.save(BytesIO(b"too big"), "x.pdf", 2)
    with pytest.raises(ValueError):
        storage.save_bytes("../escape", b"no")
    with pytest.raises(RuntimeError), storage.materialize(uri):
        raise RuntimeError("parser failed")
    if isinstance(storage, S3ObjectStorage):
        assert not list(storage.temp_dir.iterdir())


def test_unavailable_is_not_missing(tmp_path):
    client = MemoryS3()

    def denied(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    client.get_object = denied
    storage = S3ObjectStorage(Settings(_env_file=None, storage_temp_dir=tmp_path), client)
    with pytest.raises(StorageUnavailableError):
        storage.open("x")


def test_transfer_repeats_and_rejects_corruption(tmp_path):
    source = LocalObjectStorage(tmp_path / "source")
    target = LocalObjectStorage(tmp_path / "target")
    key, sha, size = source.save_bytes("x", b"hello")
    entries = [{"key": key, "old": key, "sha256": sha, "size": size}]
    transfer(entries, source, target, True)
    transfer(entries, source, target, True)
    target.resolve(key).write_bytes(b"broken")
    with pytest.raises(ValueError):
        transfer(entries, source, target, False)


@pytest.mark.parametrize("disconnect", [False, True])
def test_download_closes_body(disconnect):
    import asyncio

    from app.api.object_download import ObjectDownloadResponse

    stream = BytesIO(b"file")
    response = ObjectDownloadResponse(stream, "中文.pdf")
    assert "UTF-8" in response.headers["content-disposition"]

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if disconnect:
            raise OSError("connection lost")

    async def run():
        try:
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        except Exception:
            if not disconnect:
                raise

    asyncio.run(run())
    assert stream.closed


def test_conditional_conflict_retries_without_overwrite(tmp_path):
    client = MemoryS3()
    original = client.put_object
    calls = []

    def conflict_once(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            kwargs["Body"].read()
            raise ClientError({"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject")
        return original(**kwargs)

    client.put_object = conflict_once
    storage = S3ObjectStorage(Settings(_env_file=None, storage_temp_dir=tmp_path), client)
    storage.save_bytes("retry", b"complete body")
    assert storage.read_bytes("retry") == b"complete body"
    assert len(calls) == 2


@pytest.mark.parametrize("problem", ["missing", "corrupt", "escape"])
def test_inventory_blocks_bad_source(tmp_path, problem):
    import hashlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.commands.storage_migrate import inventory
    from app.db import Base
    from app.db.models import Document, DocumentVersion, Project

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    local = LocalObjectStorage(tmp_path / "source")
    local.save_bytes("file.md", b"valid")
    with Session(engine) as db:
        document = Document(project=Project(name="test"), filename="file.md", document_type="text")
        version = DocumentVersion(
            document=document,
            storage_path="file.md",
            sha256=hashlib.sha256(b"valid").hexdigest(),
            lifecycle_status="approved",
        )
        db.add(version)
        db.commit()
        assert len(inventory(db, local)) == 1
        if problem == "missing":
            local.delete("file.md")
        elif problem == "corrupt":
            local.resolve("file.md").write_bytes(b"bad")
        else:
            version.storage_path = "../outside"
        with pytest.raises((ValueError, FileNotFoundError)):
            inventory(db, local)
