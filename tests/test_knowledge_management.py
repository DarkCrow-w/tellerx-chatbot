from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.application.document_service import DocumentApplicationService
from app.application.errors import ResourceConflictError, ResourceNotFoundError
from app.db import Base
from app.db.models import Document, DocumentVersion, IngestionJob, OutboxEvent
from app.repositories.documents import DocumentRepository


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


if __name__ == "__main__":
    unittest.main()
