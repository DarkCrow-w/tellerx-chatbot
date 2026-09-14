"""Scan boundaries, durable task exclusion and repeated imports."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.application.scan_service import (
    KnowledgeScanner,
    ignored_reason,
    latest_scan,
    scan_prefix,
    start_scan,
)
from app.core.config import Settings
from app.db import Base
from app.db.models import KnowledgeScan, Project, utcnow
from app.integrations.s3_storage import S3ObjectStorage


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def settings():
    return SimpleNamespace(storage_backend="s3", run_inline_ingestion=True, max_upload_bytes=100)


def test_one_active_scan_and_expired_retry(db):
    project = Project(name="knowledge")
    db.add(project)
    db.commit()
    first, created = start_scan(db, project.id, settings())
    assert created
    again, created = start_scan(db, project.id, settings())
    assert not created and again.id == first.id
    first.updated_at = utcnow() - timedelta(hours=2)
    db.commit()
    assert latest_scan(db, project.id).status == "failed"
    retry, created = start_scan(db, project.id, settings())
    assert created and retry.id != first.id


@pytest.mark.parametrize(
    "key,size", [("", 0), ("folder/", 0), (".hidden/a.md", 1), ("a.exe", 1), ("a.pdf", 101)]
)
def test_unsupported_entries_skipped(key, size):
    assert ignored_reason(key, size, 100)


def test_valid_nested_chinese_file():
    assert ignored_reason("业务/说明.PDF", 10, 100) is None


def test_listing_paginates_with_prefix(tmp_path):
    client = Mock()
    client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": "imports/x/a.md"}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        },
        {"Contents": [{"Key": "imports/x/b.md"}], "IsTruncated": False},
    ]
    storage = S3ObjectStorage(Settings(_env_file=None, storage_temp_dir=tmp_path), client)
    assert len(list(storage.list_objects("imports/x/"))) == 2
    assert client.list_objects_v2.call_args.kwargs["Prefix"] == "imports/x/"
    assert client.list_objects_v2.call_args.kwargs["ContinuationToken"] == "next"


def test_one_bad_file_does_not_stop_scan(db):
    project = Project(name="knowledge")
    db.add(project)
    db.commit()
    scan, _ = start_scan(db, project.id, settings())
    prefix = scan_prefix(project.id)
    storage = Mock()
    storage.list_objects.return_value = [{"Key": prefix + "bad.md"}, {"Key": prefix + "good.md"}]
    scanner = KnowledgeScanner(settings(), storage, Mock())
    scanner.process_object = Mock(side_effect=[ValueError("bad"), ("imported", "ok")])
    scanner.run(db, scan.id)
    db.refresh(scan)
    assert (scan.status, scan.processed, scan.failed, scan.imported) == (
        "completed_with_errors",
        2,
        1,
        1,
    )
    assert scan.details[0]["key"] == prefix + "bad.md"


def test_listing_failure_reported(db):
    project = Project(name="knowledge")
    db.add(project)
    db.commit()
    scan, _ = start_scan(db, project.id, settings())
    storage = Mock()
    storage.list_objects.side_effect = RuntimeError("offline")
    KnowledgeScanner(settings(), storage, Mock()).run(db, scan.id)
    assert db.get(KnowledgeScan, scan.id).status == "failed"


def test_scan_migration_upgrade_and_downgrade():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    spec = importlib.util.spec_from_file_location(
        "scan_migration", Path("alembic/versions/0002_knowledge_scans.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
        assert inspect(connection).has_table("knowledge_scans")
        module.downgrade()
        assert not inspect(connection).has_table("knowledge_scans")
