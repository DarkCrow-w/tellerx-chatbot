from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.application.document_service import DocumentApplicationService
from app.application.errors import ResourceConflictError, ResourceNotFoundError
from app.commands.reindex import _eligible_versions
from app.db import Base
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentAcl,
    DocumentArtifact,
    DocumentVersion,
    EmbeddingCache,
    EmbeddingModel,
    IndexGeneration,
    IndexSyncState,
    IngestionJob,
    OutboxEvent,
    Principal,
    Project,
)
from app.repositories.documents import DocumentRepository
from app.services.ingestion import IngestionService


class RecordingStorage:
    """记录清理请求的内存存储替身。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, object_path: str) -> bool:
        self.deleted.append(object_path)
        return True


class KnowledgeManagementTest(unittest.TestCase):
    """验证管理接口依赖的项目和文档聚合语义。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        settings = SimpleNamespace(
            max_upload_bytes=100 * 1024 * 1024,
            run_inline_ingestion=False,
            storage_root=Path("."),
        )
        self.service = DocumentApplicationService(
            settings,
            storage=None,
            repository=DocumentRepository(),
            ingestion=None,
        )

    def use_recording_storage(self) -> RecordingStorage:
        storage = RecordingStorage()
        self.service.storage = storage
        return storage

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_create_rename_and_duplicate_project(self) -> None:
        project = self.service.create_project(self.db, "内部制度")
        renamed = self.service.rename_project(self.db, project.id, "内部知识库")
        self.assertEqual(renamed.name, "内部知识库")
        with self.assertRaises(ResourceConflictError):
            self.service.create_project(self.db, "内部知识库")

    def test_document_page_contains_latest_current_and_job(self) -> None:
        project = self.service.create_project(self.db, "内部知识库")
        document = Document(
            project_id=project.id,
            logical_key="财务/报销.md",
            filename="报销.md",
            document_type="text-document",
        )
        self.db.add(document)
        self.db.flush()
        current = DocumentVersion(
            document_id=document.id,
            sha256="a" * 64,
            storage_path="old",
            lifecycle_status="approved",
            technical_status="searchable",
            is_current=True,
            version_label="1.0",
        )
        latest = DocumentVersion(
            document_id=document.id,
            sha256="b" * 64,
            storage_path="new",
            lifecycle_status="approved",
            technical_status="failed_final",
            is_current=False,
            version_label="2.0",
        )
        self.db.add_all([current, latest])
        self.db.flush()
        job = IngestionJob(
            document_id=document.id,
            version_id=latest.id,
            status="failed",
            stage="embedding",
            progress=40,
            error_message="mock failure",
        )
        self.db.add(job)
        self.db.commit()

        page = self.service.list_documents(
            self.db,
            project_id=project.id,
            query="报销",
            limit=20,
            offset=0,
        )

        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].current_version.id, current.id)
        self.assertEqual(page.items[0].latest_version.id, latest.id)
        self.assertEqual(page.items[0].latest_job.id, job.id)
        self.assertEqual(page.items[0].latest_job.error_message, "mock failure")

    def test_equal_timestamp_versions_use_id_as_stable_tiebreaker(self) -> None:
        project = self.service.create_project(self.db, "时间戳并列知识库")
        document = Document(
            project_id=project.id,
            logical_key="并列版本.md",
            filename="并列版本.md",
            document_type="text-document",
        )
        self.db.add(document)
        self.db.flush()
        same_time = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
        lower_id = "00000000-0000-4000-8000-000000000001"
        higher_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        older_by_tiebreaker = DocumentVersion(
            id=lower_id,
            document_id=document.id,
            sha256="c" * 64,
            storage_path="same-time-lower-id",
            lifecycle_status="approved",
            technical_status="searchable",
            is_current=True,
            created_at=same_time,
        )
        newest_by_tiebreaker = DocumentVersion(
            id=higher_id,
            document_id=document.id,
            sha256="d" * 64,
            storage_path="same-time-higher-id",
            lifecycle_status="approved",
            technical_status="received",
            is_current=False,
            created_at=same_time,
        )
        self.db.add_all([older_by_tiebreaker, newest_by_tiebreaker])
        self.db.commit()

        page = self.service.list_documents(
            self.db,
            project_id=project.id,
            query=None,
            limit=20,
            offset=0,
        )
        versions = self.service.list_versions(self.db, document.id)
        download_version = self.service.repository.get_download_version(
            self.db,
            document_id=document.id,
            version_id=None,
        )

        self.assertEqual(page.items[0].latest_version.id, higher_id)
        self.assertEqual([version.id for version in versions], [higher_id, lower_id])
        self.assertEqual(download_version.id, higher_id)

    def test_search_soft_delete_and_missing_project(self) -> None:
        project = self.service.create_project(self.db, "制度库")
        self.db.add(
            Document(
                project_id=project.id,
                logical_key="隐藏.md",
                filename="隐藏.md",
                document_type="text-document",
                is_deleted=True,
            )
        )
        self.db.commit()
        page = self.service.list_documents(
            self.db,
            project_id=project.id,
            query=None,
            limit=20,
            offset=0,
        )
        self.assertEqual(page.total, 0)
        with self.assertRaises(ResourceNotFoundError):
            self.service.list_documents(
                self.db,
                project_id="missing",
                query=None,
                limit=20,
                offset=0,
            )

    def test_capabilities_come_from_backend_configuration(self) -> None:
        capabilities = self.service.document_capabilities()
        self.assertIn(".pdf", capabilities.allowed_extensions)
        self.assertEqual(capabilities.max_upload_bytes, 100 * 1024 * 1024)
        self.assertEqual(capabilities.default_lifecycle_status, "approved")

    def test_bulk_delete_is_scoped_deduplicated_and_soft_deletes(self) -> None:
        project = self.service.create_project(self.db, "待清理知识库")
        other_project = self.service.create_project(self.db, "其他知识库")
        selected = Document(
            project_id=project.id,
            logical_key="selected.md",
            filename="selected.md",
            document_type="text-document",
        )
        retained = Document(
            project_id=project.id,
            logical_key="retained.md",
            filename="retained.md",
            document_type="text-document",
        )
        foreign = Document(
            project_id=other_project.id,
            logical_key="foreign.md",
            filename="foreign.md",
            document_type="text-document",
        )
        self.db.add_all([selected, retained, foreign])
        self.db.flush()
        versions = [
            DocumentVersion(
                document_id=selected.id,
                sha256=character * 64,
                storage_path=f"selected-{character}",
                lifecycle_status="approved",
                technical_status="searchable",
                is_current=position == 0,
            )
            for position, character in enumerate(("a", "b"))
        ]
        self.db.add_all(versions)
        self.db.commit()

        result = self.service.bulk_delete_documents(
            self.db,
            project_id=project.id,
            document_ids=[selected.id, "missing", foreign.id, selected.id],
        )

        self.assertEqual(result.requested_count, 3)
        self.assertEqual(result.deleted_ids, [selected.id])
        self.assertEqual(result.skipped_ids, ["missing", foreign.id])
        self.assertTrue(self.db.get(Document, selected.id).is_deleted)
        self.assertFalse(self.db.get(Document, retained.id).is_deleted)
        self.assertFalse(self.db.get(Document, foreign.id).is_deleted)
        self.assertTrue(all(not version.is_current for version in versions))
        events = list(self.db.scalars(select(OutboxEvent)))
        self.assertEqual({event.aggregate_id for event in events}, {version.id for version in versions})

        page = self.service.list_documents(
            self.db,
            project_id=project.id,
            query=None,
            limit=20,
            offset=0,
        )
        self.assertEqual([document.id for document in page.items], [retained.id])

    def test_bulk_delete_rejects_missing_project(self) -> None:
        with self.assertRaises(ResourceNotFoundError):
            self.service.bulk_delete_documents(
                self.db,
                project_id="missing",
                document_ids=["document"],
            )

    def test_soft_delete_cancels_jobs_and_prevents_reindexing_deleted_drafts(self) -> None:
        project = self.service.create_project(self.db, "软删除知识库")
        document = Document(
            project_id=project.id,
            logical_key="draft.md",
            filename="draft.md",
            document_type="text-document",
        )
        self.db.add(document)
        self.db.flush()
        version = DocumentVersion(
            document_id=document.id,
            sha256="e" * 64,
            storage_path="draft-source",
            lifecycle_status="draft",
            technical_status="searchable",
        )
        self.db.add(version)
        self.db.flush()
        job = IngestionJob(document_id=document.id, version_id=version.id, status="queued")
        pending_event = OutboxEvent(
            aggregate_id=version.id,
            event_type="index_version",
            payload={"job_id": job.id},
        )
        self.db.add_all([job, pending_event])
        active_document = Document(
            project_id=project.id,
            logical_key="active.md",
            filename="active.md",
            document_type="text-document",
        )
        self.db.add(active_document)
        self.db.flush()
        active_version = DocumentVersion(
            document_id=active_document.id,
            sha256="0" * 64,
            storage_path="active-source",
            lifecycle_status="draft",
            technical_status="searchable",
        )
        self.db.add(active_version)
        self.db.flush()
        active_job = IngestionJob(
            document_id=active_document.id,
            version_id=active_version.id,
            status="queued",
        )
        self.db.add(active_job)
        self.db.commit()

        self.service.delete_document(self.db, document.id)

        self.assertTrue(document.is_deleted)
        self.assertEqual(version.technical_status, "deleted")
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(pending_event.status, "superseded")
        self.assertEqual([item.id for item in _eligible_versions(self.db)], [active_version.id])
        worker = object.__new__(IngestionService)
        self.assertEqual(worker.claim_next_job(self.db), active_job.id)

    def test_cleanup_project_removes_deleted_residue_and_keeps_active_data(self) -> None:
        storage = self.use_recording_storage()
        target = self.service.create_project(self.db, "待清空知识库")
        retained = self.service.create_project(self.db, "保留知识库")
        target_document = Document(
            project_id=target.id,
            logical_key="target.md",
            filename="target.md",
            document_type="text-document",
        )
        deleted_document = Document(
            project_id=target.id,
            logical_key="deleted.md",
            filename="deleted.md",
            document_type="text-document",
            is_deleted=True,
        )
        retained_document = Document(
            project_id=retained.id,
            logical_key="retained.md",
            filename="retained.md",
            document_type="text-document",
        )
        self.db.add_all([target_document, deleted_document, retained_document])
        self.db.flush()
        target_version = DocumentVersion(
            document_id=target_document.id,
            sha256="f" * 64,
            storage_path="shared-source",
            lifecycle_status="approved",
            technical_status="searchable",
            is_current=True,
        )
        deleted_version = DocumentVersion(
            document_id=deleted_document.id,
            sha256="1" * 64,
            storage_path="deleted-source",
            lifecycle_status="draft",
            technical_status="deleted",
        )
        retained_version = DocumentVersion(
            document_id=retained_document.id,
            sha256="2" * 64,
            storage_path="shared-source",
            lifecycle_status="approved",
            technical_status="searchable",
            is_current=True,
        )
        self.db.add_all([target_version, deleted_version, retained_version])
        self.db.flush()
        target_chunk = Chunk(
            version_id=target_version.id,
            ordinal=0,
            content="shared",
            content_hash="3" * 64,
            record_hash="4" * 64,
            token_count=1,
        )
        deleted_chunk = Chunk(
            version_id=deleted_version.id,
            ordinal=0,
            content="unique",
            content_hash="5" * 64,
            record_hash="6" * 64,
            token_count=1,
        )
        retained_chunk = Chunk(
            version_id=retained_version.id,
            ordinal=0,
            content="shared",
            content_hash="3" * 64,
            record_hash="7" * 64,
            token_count=1,
        )
        self.db.add_all([target_chunk, deleted_chunk, retained_chunk])
        model = EmbeddingModel(
            fingerprint="model-fingerprint",
            model_id="embedding-model",
            dimensions=2560,
            preprocess_version="v1",
        )
        shared_cache = EmbeddingCache(
            content_hash="3" * 64,
            embedding_fingerprint=model.fingerprint,
            object_uri="shared-vector",
            checksum="8" * 64,
            dimensions=2560,
        )
        unique_cache = EmbeddingCache(
            content_hash="5" * 64,
            embedding_fingerprint=model.fingerprint,
            object_uri="unique-vector",
            checksum="9" * 64,
            dimensions=2560,
        )
        self.db.add_all([model, shared_cache, unique_cache])
        self.db.flush()
        self.db.add_all(
            [
                ChunkEmbedding(
                    chunk_id=target_chunk.id,
                    embedding_fingerprint=model.fingerprint,
                    cache_id=shared_cache.id,
                ),
                ChunkEmbedding(
                    chunk_id=deleted_chunk.id,
                    embedding_fingerprint=model.fingerprint,
                    cache_id=unique_cache.id,
                ),
                ChunkEmbedding(
                    chunk_id=retained_chunk.id,
                    embedding_fingerprint=model.fingerprint,
                    cache_id=shared_cache.id,
                ),
                DocumentArtifact(
                    version_id=deleted_version.id,
                    artifact_type="normalized",
                    object_uri="target-artifact",
                    sha256="a" * 64,
                    byte_size=10,
                    fingerprint="parser-v1",
                ),
                IngestionJob(
                    document_id=deleted_document.id,
                    version_id=deleted_version.id,
                    status="succeeded",
                ),
                OutboxEvent(
                    aggregate_id=deleted_version.id,
                    event_type="index_version",
                    payload={},
                ),
            ]
        )
        principal = Principal(principal_type="user", external_id="cleanup-test")
        generation = IndexGeneration(
            physical_index="cleanup-test-index",
            schema_version="1",
            embedding_fingerprint=model.fingerprint,
        )
        self.db.add_all([principal, generation])
        self.db.flush()
        self.db.add_all(
            [
                DocumentAcl(document_id=deleted_document.id, principal_id=principal.id),
                IndexSyncState(
                    version_id=deleted_version.id,
                    generation_id=generation.id,
                    manifest_hash="b" * 64,
                ),
            ]
        )
        self.db.commit()

        result = self.service.cleanup_project(self.db, target.id)

        self.assertFalse(result.project_deleted)
        self.assertEqual(result.documents_deleted, 1)
        self.assertEqual(result.versions_deleted, 1)
        self.assertEqual(result.chunks_deleted, 1)
        self.assertEqual(result.embedding_cache_deleted, 1)
        self.assertIsNotNone(self.db.get(Project, target.id))
        self.assertIsNotNone(self.db.get(Document, target_document.id))
        self.assertIsNone(self.db.get(Document, deleted_document.id))
        self.assertIsNotNone(self.db.get(Document, retained_document.id))
        self.assertIsNotNone(self.db.get(EmbeddingCache, shared_cache.id))
        self.assertIsNone(self.db.get(EmbeddingCache, unique_cache.id))
        self.assertEqual(
            set(storage.deleted),
            {"deleted-source", "target-artifact", "unique-vector"},
        )
        self.assertNotIn("shared-source", storage.deleted)
        self.assertNotIn("shared-vector", storage.deleted)

    def test_delete_project_removes_empty_project_and_missing_project_is_rejected(self) -> None:
        self.use_recording_storage()
        project = self.service.create_project(self.db, "空知识库")

        result = self.service.delete_project(self.db, project.id)

        self.assertTrue(result.project_deleted)
        self.assertIsNone(self.db.get(Project, project.id))
        with self.assertRaises(ResourceNotFoundError):
            self.service.delete_project(self.db, "missing")


if __name__ == "__main__":
    unittest.main()
