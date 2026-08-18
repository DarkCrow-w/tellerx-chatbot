from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
    IndexSyncState,
    IngestionJob,
    OutboxEvent,
)
from app.search import SearchIndex
from app.storage import LocalObjectStorage

logger = logging.getLogger(__name__)


def version_manifest_hash(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda item: item.ordinal):
        digest.update(f"{chunk.ordinal}:{chunk.id}:{chunk.record_hash}\n".encode())
    return digest.hexdigest()


class IndexingService:
    def __init__(
        self,
        settings: Settings,
        index: SearchIndex,
        storage: LocalObjectStorage | None = None,
    ):
        self.settings = settings
        self.index = index
        self.storage = storage or LocalObjectStorage(settings.storage_root)

    def ensure_generation(self, db: Session) -> IndexGeneration:
        physical = self.index.current_write_index()
        generation = db.scalar(
            select(IndexGeneration).where(IndexGeneration.physical_index == physical)
        )
        if not generation:
            generation = IndexGeneration(
                physical_index=physical,
                schema_version=str(self.settings.elasticsearch_schema_version),
                embedding_fingerprint=self.settings.embedding_fingerprint,
                status="active",
                activated_at=datetime.now(UTC),
            )
            db.add(generation)
            db.flush()
        return generation

    def _refresh_generation_counts(self, db: Session) -> None:
        generation = self.ensure_generation(db)
        generation.expected_chunks = int(
            db.scalar(
                select(func.count())
                .select_from(Chunk)
                .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
                .where(
                    DocumentVersion.technical_status == "searchable",
                    (
                        (DocumentVersion.lifecycle_status == "draft")
                        | (
                            (DocumentVersion.lifecycle_status == "approved")
                            & (DocumentVersion.is_current.is_(True))
                        )
                    ),
                )
            )
            or 0
        )
        generation.indexed_chunks = self.index.count_all()

    @staticmethod
    def _retire_sync_states(db: Session, version_id: str) -> None:
        for sync in db.scalars(
            select(IndexSyncState).where(IndexSyncState.version_id == version_id)
        ):
            sync.status = "retired"
            sync.indexed_chunks = 0

    def claim_next_event(self, db: Session) -> str | None:
        now = datetime.now(UTC)
        event = db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.status == "pending",
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not event:
            db.rollback()
            return None
        event.status = "processing"
        event.attempts += 1
        event.lease_until = now + timedelta(minutes=5)
        db.commit()
        return event.id

    def recover_expired_leases(self, db: Session) -> int:
        now = datetime.now(UTC)
        events = list(
            db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.status == "processing",
                    OutboxEvent.lease_until < now,
                )
            )
        )
        for event in events:
            event.status = "pending"
            event.lease_until = None
        if events:
            db.commit()
        return len(events)

    def _documents_for_version(self, db: Session, version_id: str) -> tuple[DocumentVersion, list[dict]]:
        version = db.scalar(
            select(DocumentVersion)
            .options(joinedload(DocumentVersion.document).joinedload(Document.project))
            .where(DocumentVersion.id == version_id)
        )
        if not version:
            raise ValueError(f"Unknown document version: {version_id}")
        document = version.document
        chunks = list(
            db.scalars(select(Chunk).where(Chunk.version_id == version_id).order_by(Chunk.ordinal))
        )
        cache_by_chunk = {
            chunk_id: cache
            for chunk_id, cache in db.execute(
                select(ChunkEmbedding.chunk_id, EmbeddingCache)
                .join(EmbeddingCache, ChunkEmbedding.cache_id == EmbeddingCache.id)
                .where(
                    ChunkEmbedding.chunk_id.in_([chunk.id for chunk in chunks]),
                    ChunkEmbedding.embedding_fingerprint == self.settings.embedding_fingerprint,
                )
            ).all()
        } if chunks else {}
        rows = []
        for chunk in chunks:
            row = {
                "chunk_id": chunk.id,
                "document_id": document.id,
                "version_id": version.id,
                "project_id": document.project.id,
                "filename": document.filename,
                "lifecycle_status": version.lifecycle_status,
                "document_type": document.document_type,
                "visibility": document.visibility,
                "version_label": version.version_label,
                "effective_from": version.effective_at,
                "effective_to": version.effective_to,
                "is_current": version.lifecycle_status == "approved",
                "is_searchable": version.lifecycle_status != "deprecated",
                "title_path": chunk.heading_path,
                "page_number": chunk.page_number,
                "sheet_name": chunk.sheet_name,
                "cell_range": chunk.cell_range,
                "chunk_ordinal": chunk.ordinal,
                "parent_chunk_id": chunk.parent_chunk_id,
                "previous_chunk_id": chunk.previous_chunk_id,
                "next_chunk_id": chunk.next_chunk_id,
                "content": chunk.content,
                "content_hash": chunk.content_hash,
                "record_hash": chunk.record_hash,
            }
            cache = cache_by_chunk.get(chunk.id)
            if cache:
                row["embedding"] = self.storage.load_vector(
                    cache.object_uri, cache.dimensions, cache.checksum
                )
            rows.append(row)
        return version, rows

    def publish_event(self, db: Session, event_id: str) -> bool:
        event = db.get(OutboxEvent, event_id)
        if not event:
            raise ValueError(f"Unknown outbox event: {event_id}")
        if event.status == "published":
            return True
        if event.status == "pending":
            event.status = "processing"
            event.attempts += 1
            event.lease_until = datetime.now(UTC) + timedelta(minutes=5)
            db.commit()
        try:
            if event.event_type == "delete_version":
                self.index.delete_version(event.aggregate_id)
                version = db.get(DocumentVersion, event.aggregate_id)
                if version:
                    version.technical_status = "deleted"
                    version.is_current = False
                    self._retire_sync_states(db, version.id)
                self._refresh_generation_counts(db)
                job_id = (event.payload or {}).get("job_id")
                job = db.get(IngestionJob, job_id) if job_id else None
                if job:
                    job.status = "succeeded"
                    job.stage = "complete"
                    job.progress = 100
                    job.error_message = None
                    job.finished_at = datetime.now(UTC)
            elif event.event_type == "index_version":
                self._publish_version(db, event)
            else:
                raise ValueError(f"Unsupported outbox event type: {event.event_type}")
            event = db.get(OutboxEvent, event_id)
            event.status = "published"
            event.published_at = datetime.now(UTC)
            event.lease_until = None
            event.last_error = None
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            event = db.get(OutboxEvent, event_id)
            if event:
                event.status = "dead" if event.attempts >= 5 else "pending"
                event.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2 ** min(event.attempts, 8))
                )
                event.lease_until = None
                event.last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                job_id = (event.payload or {}).get("job_id")
                job = db.get(IngestionJob, job_id) if job_id else None
                if job:
                    job.status = "failed" if event.status == "dead" else "index_pending"
                    job.stage = "indexing_failed" if event.status == "dead" else "indexing_retry"
                    job.error_message = event.last_error
                db.commit()
            logger.exception("Could not publish outbox event %s", event_id)
            return False

    def _publish_version(self, db: Session, event: OutboxEvent) -> None:
        version, documents = self._documents_for_version(db, event.aggregate_id)
        if not documents:
            raise ValueError(f"Version {version.id} has no chunks")
        self.index.index_chunks(documents)
        self.index.delete_stale_version_chunks(
            version.id, [document["chunk_id"] for document in documents]
        )
        actual = self.index.count_version(version.id)
        expected = len(documents)
        if actual != expected:
            raise RuntimeError(
                f"Elasticsearch verification failed for {version.id}: expected={expected}, actual={actual}"
            )

        now = datetime.now(UTC)
        chunks = list(
            db.scalars(select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal))
        )
        manifest = version_manifest_hash(chunks)
        generation = self.ensure_generation(db)
        sync = db.scalar(
            select(IndexSyncState).where(
                IndexSyncState.version_id == version.id,
                IndexSyncState.generation_id == generation.id,
            )
        )
        if not sync:
            sync = IndexSyncState(
                version_id=version.id,
                generation_id=generation.id,
                manifest_hash=manifest,
            )
            db.add(sync)
        sync.expected_chunks = expected
        sync.indexed_chunks = actual
        sync.manifest_hash = manifest
        sync.status = "verified"
        sync.verified_at = now
        sync.last_error = None

        will_be_current = version.lifecycle_status == "approved"
        older: list[DocumentVersion] = []
        if will_be_current:
            older = list(
                db.scalars(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == version.document_id,
                        DocumentVersion.id != version.id,
                        DocumentVersion.is_current.is_(True),
                    )
                )
            )
            for old_version in older:
                old_version.is_current = False
                old_version.lifecycle_status = "deprecated"
                old_version.effective_to = now
                old_version.technical_status = "superseded"
            # Release the partial unique slot before setting the replacement
            # current, while keeping the whole switch in one DB transaction.
            db.flush()

        version.indexed_at = now
        version.searchable_at = now
        version.technical_status = "searchable"
        version.is_current = will_be_current
        db.flush()
        for old_version in older:
            self.index.delete_version(old_version.id)
            self._retire_sync_states(db, old_version.id)

        self._refresh_generation_counts(db)
        job_id = (event.payload or {}).get("job_id")
        job = db.get(IngestionJob, job_id) if job_id else None
        if job:
            job.status = "succeeded"
            job.stage = "complete"
            job.progress = 100
            job.error_message = None
            job.finished_at = now

    def reconcile(self, db: Session, *, repair: bool = False) -> dict[str, object]:
        generation = self.ensure_generation(db)
        versions = list(
            db.scalars(
                select(DocumentVersion).where(
                    DocumentVersion.technical_status == "searchable",
                    (
                        (DocumentVersion.lifecycle_status == "draft")
                        | (
                            (DocumentVersion.lifecycle_status == "approved")
                            & (DocumentVersion.is_current.is_(True))
                        )
                    ),
                )
            )
        )
        differences = []
        repaired = 0
        for version in versions:
            expected = int(
                db.scalar(
                    select(func.count()).select_from(Chunk).where(Chunk.version_id == version.id)
                )
                or 0
            )
            chunks = list(
                db.scalars(
                    select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal)
                )
            )
            expected_manifest = version_manifest_hash(chunks)
            records = self.index.version_records(version.id)
            actual = len(records)
            actual_manifest_digest = hashlib.sha256()
            for ordinal, chunk_id, record_hash in records:
                actual_manifest_digest.update(
                    f"{ordinal}:{chunk_id}:{record_hash}\n".encode()
                )
            actual_manifest = actual_manifest_digest.hexdigest()
            sync = db.scalar(
                select(IndexSyncState).where(
                    IndexSyncState.version_id == version.id,
                    IndexSyncState.generation_id == generation.id,
                )
            )
            if not sync:
                sync = IndexSyncState(
                    version_id=version.id,
                    generation_id=generation.id,
                    manifest_hash=expected_manifest,
                )
                db.add(sync)
            sync.expected_chunks = expected
            sync.indexed_chunks = actual
            sync.manifest_hash = expected_manifest
            if expected == actual and expected_manifest == actual_manifest:
                sync.status = "verified"
                sync.verified_at = datetime.now(UTC)
                sync.last_error = None
                continue
            sync.status = "mismatch"
            sync.last_error = "Elasticsearch count or record manifest differs from PostgreSQL"
            differences.append(
                {
                    "version_id": version.id,
                    "expected_chunks": expected,
                    "indexed_chunks": actual,
                    "manifest_matches": expected_manifest == actual_manifest,
                }
            )
            if repair:
                event = OutboxEvent(
                    aggregate_id=version.id,
                    event_type="index_version",
                    payload={"reason": "reconcile"},
                )
                db.add(event)
                db.commit()
                repaired += int(self.publish_event(db, event.id))
        self._refresh_generation_counts(db)
        db.commit()
        return {
            "checked_versions": len(versions),
            "difference_count": len(differences),
            "repaired": repaired,
            "differences": differences,
        }
