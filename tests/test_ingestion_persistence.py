from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.ingestion import IngestionService
from app.models import (
    Chunk,
    Document,
    DocumentArtifact,
    DocumentVersion,
    EmbeddingCache,
    IngestionJob,
    OutboxEvent,
    Project,
)
from app.parsers import DocumentParser


class CountingQwen:
    def __init__(self) -> None:
        self.calls = 0

    def embeddings(self, texts: list[str]):
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts], SimpleNamespace()


class MemoryIndex:
    def __init__(self) -> None:
        self.by_version: dict[str, list[dict]] = {}

    def delete_version(self, version_id: str) -> None:
        self.by_version.pop(version_id, None)

    def index_chunks(self, documents: list[dict], **_: object) -> None:
        for document in documents:
            rows = self.by_version.setdefault(document["version_id"], [])
            rows[:] = [row for row in rows if row["chunk_id"] != document["chunk_id"]]
            rows.append(document)

    def count_version(self, version_id: str, **_: object) -> int:
        return len(self.by_version.get(version_id, []))

    def count_all(self, **_: object) -> int:
        return sum(len(rows) for rows in self.by_version.values())

    def current_write_index(self) -> str:
        return "memory-index"

    def current_read_index(self) -> str:
        return "memory-index"

    def trace_index_name(self) -> str:
        return "memory-index"

    def version_records(self, version_id: str) -> list[tuple[int, str, str]]:
        return sorted(
            (
                int(row["chunk_ordinal"]),
                str(row["chunk_id"]),
                str(row["record_hash"]),
            )
            for row in self.by_version.get(version_id, [])
        )

    def delete_stale_version_chunks(
        self, version_id: str, current_chunk_ids: list[str]
    ) -> None:
        self.by_version[version_id] = [
            row
            for row in self.by_version.get(version_id, [])
            if row["chunk_id"] in current_chunk_ids
        ]


def _add_job(
    db: Session, path: Path, suffix: str, lifecycle_status: str = "draft"
) -> IngestionJob:
    project = db.scalar(select(Project).where(Project.name == "P"))
    if not project:
        project = Project(name="P")
        db.add(project)
        db.flush()
    document = db.scalar(select(Document).where(Document.logical_key == "rule"))
    if not document:
        document = Document(
            project_id=project.id,
            logical_key="rule",
            filename="rule.txt",
            document_type="requirement",
        )
        db.add(document)
        db.flush()
    version = DocumentVersion(
        document_id=document.id,
        sha256=f"hash-{suffix}",
        storage_path=path.name,
        lifecycle_status=lifecycle_status,
    )
    db.add(version)
    db.flush()
    job = IngestionJob(document_id=document.id, version_id=version.id)
    db.add(job)
    db.commit()
    return job


def test_ingestion_persists_artifact_embedding_and_reuses_cache(tmp_path: Path) -> None:
    source = tmp_path / "rule.txt"
    source.write_text("KBR-0001 approval threshold is CNY 5000.", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        storage_root=tmp_path,
        qwen_embedding_model="test-embedding",
        qwen_embedding_dimensions=3,
        allow_bm25_only=False,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    event.listen(
        engine,
        "connect",
        lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"),
    )
    Base.metadata.create_all(engine)
    qwen = CountingQwen()
    index = MemoryIndex()
    service = IngestionService(
        settings,
        DocumentParser("native"),
        qwen,  # type: ignore[arg-type]
        index,  # type: ignore[arg-type]
    )

    with Session(engine) as db:
        first = _add_job(db, source, "one")
        first_event = service.process(db, first.id)
        assert db.get(IngestionJob, first.id).status == "index_pending"
        assert index.by_version == {}
        service.indexer.publish_event(db, first_event)
        second = _add_job(db, source, "two")
        second_event = service.process(db, second.id)
        service.indexer.publish_event(db, second_event)

        assert qwen.calls == 1
        assert db.scalar(select(func.count()).select_from(EmbeddingCache)) == 1
        assert db.scalar(select(func.count()).select_from(DocumentArtifact)) == 2
        assert db.scalar(select(func.count()).select_from(Chunk)) == 2
        assert set(db.scalars(select(IngestionJob.status))) == {"succeeded"}
        assert set(db.scalars(select(OutboxEvent.status))) == {"published"}
        assert set(db.scalars(select(DocumentVersion.technical_status))) == {"searchable"}


def test_verified_approved_version_replaces_previous_version_without_early_delete(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rule.txt"
    source.write_text("KBR-0001 approval threshold is CNY 5000.", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        storage_root=tmp_path,
        qwen_embedding_model="test-embedding",
        qwen_embedding_dimensions=3,
        allow_bm25_only=False,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    index = MemoryIndex()
    service = IngestionService(
        settings,
        DocumentParser("native"),
        CountingQwen(),  # type: ignore[arg-type]
        index,  # type: ignore[arg-type]
    )

    with Session(engine) as db:
        first_job = _add_job(db, source, "one", "approved")
        first_event = service.process(db, first_job.id)
        service.indexer.publish_event(db, first_event)
        first = db.get(DocumentVersion, first_job.version_id)
        assert first is not None and first.is_current is True
        assert first.id in index.by_version

        second_job = _add_job(db, source, "two", "approved")
        second_event = service.process(db, second_job.id)
        service.indexer.publish_event(db, second_event)
        second = db.get(DocumentVersion, second_job.version_id)
        db.refresh(first)

        assert second is not None and second.is_current is True
        assert second.technical_status == "searchable"
        assert first.is_current is False
        assert first.lifecycle_status == "deprecated"
        assert first.technical_status == "superseded"
        assert first.id not in index.by_version
        assert second.id in index.by_version

        index.by_version[second.id][0]["record_hash"] = "corrupted"
        reconciliation = service.indexer.reconcile(db)
        assert reconciliation["difference_count"] == 1
        assert reconciliation["differences"][0]["manifest_matches"] is False
