"""Application Service 分层的关键行为回归。"""

from __future__ import annotations

from io import BytesIO

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.application.chat_service import ChatApplicationService
from app.application.document_service import DocumentApplicationService, UploadDocumentCommand
from app.application.errors import InvalidRequestError, ResourceConflictError
from app.core.config import Settings
from app.db import Base
from app.db.models import Document, DocumentVersion, OutboxEvent, Project
from app.integrations.storage import LocalObjectStorage
from app.repositories.chat import ChatRepository
from app.repositories.documents import DocumentRepository


class NoopIngestion:
    """文档应用服务测试不执行真实解析和模型调用。"""

    def process(self, db: Session, job_id: str) -> None:
        raise AssertionError(f"unexpected inline ingestion: {job_id}")


def document_service(tmp_path) -> DocumentApplicationService:
    """构造只使用临时对象存储的文档应用服务。"""

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        storage_root=tmp_path,
        run_inline_ingestion=False,
    )
    return DocumentApplicationService(
        settings,
        LocalObjectStorage(tmp_path),
        DocumentRepository(),
        NoopIngestion(),
    )


def upload_command(content: bytes = b"# policy") -> UploadDocumentCommand:
    """生成可重复读取的新上传命令。"""

    return UploadDocumentCommand(
        stream=BytesIO(content),
        filename="policy.md",
        project="Operations",
        document_type="policy",
        lifecycle_status="draft",
    )


def test_document_upload_is_idempotent_by_logical_document_and_hash(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = document_service(tmp_path)

    with Session(engine) as db:
        first = service.upload(db, upload_command())
        duplicate = service.upload(db, upload_command())

        assert first.response.duplicate is False
        assert duplicate.response.duplicate is True
        assert duplicate.response.document_id == first.response.document_id
        assert duplicate.response.version_id == first.response.version_id
        assert duplicate.response.job_id == first.response.job_id


def test_approval_publishes_outbox_only_after_version_is_searchable(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = document_service(tmp_path)

    with Session(engine) as db:
        project = Project(name="Operations")
        document = Document(
            project=project,
            logical_key="policy",
            filename="policy.md",
            document_type="policy",
        )
        version = DocumentVersion(
            document=document,
            sha256="a" * 64,
            storage_path="source.md",
            lifecycle_status="draft",
            technical_status="received",
        )
        db.add(version)
        db.commit()

        with pytest.raises(ResourceConflictError):
            service.approve_version(db, version.id)

        version.technical_status = "searchable"
        db.commit()
        approved = service.approve_version(db, version.id)

        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == version.id))
        assert approved.lifecycle_status == "approved"
        assert approved.is_current is False
        assert event is not None
        assert event.event_type == "index_version"


def test_chat_scope_validation_does_not_construct_model_clients() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    provider_called = False

    def forbidden_answering_provider():
        nonlocal provider_called
        provider_called = True
        raise AssertionError("model client must not be created for an ambiguous project scope")

    service = ChatApplicationService(forbidden_answering_provider, ChatRepository())
    with Session(engine) as db:
        db.add_all([Project(name="A"), Project(name="B")])
        db.commit()

        with pytest.raises(InvalidRequestError):
            service.answer(
                db,
                question="当前规则是什么？",
                project_ids=[],
                conversation_id=None,
                pinned_model=None,
            )

    assert provider_called is False
