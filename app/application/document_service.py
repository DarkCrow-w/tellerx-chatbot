"""文档目录、上传和生命周期应用用例。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, ClassVar, Protocol

from sqlalchemy.orm import Session

from app.application.errors import (
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
    UnsupportedDocumentError,
    UploadTooLargeError,
)
from app.contracts.schemas import SourceOut, UploadResponse
from app.core.config import Settings
from app.db.models import DocumentVersion, IngestionJob, Project
from app.knowledge.parsers import DocumentParser
from app.repositories.documents import DocumentRepository


class ObjectStorage(Protocol):
    """文档应用层需要的最小对象存储能力。"""

    def save(
        self, stream: BinaryIO, filename: str, max_bytes: int
    ) -> tuple[Path, str, int]: ...

    def resolve(self, storage_path: str) -> Path: ...


class IngestionProcessor(Protocol):
    """后台入库处理器接口。"""

    def process(self, db: Session, job_id: str) -> None: ...


@dataclass(slots=True)
class UploadDocumentCommand:
    """Controller 校验传输格式后交给应用层的上传命令。"""

    stream: BinaryIO
    filename: str
    project: str
    document_type: str
    lifecycle_status: str
    version_label: str | None = None
    logical_key: str | None = None
    effective_at: datetime | None = None
    owner: str | None = None
    supersedes_document_id: str | None = None


@dataclass(frozen=True, slots=True)
class UploadDocumentResult:
    """上传结果以及开发模式下需要内联执行的任务。"""

    response: UploadResponse
    inline_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadDocumentResult:
    """下载端点构造文件响应所需的信息。"""

    path: Path
    filename: str


class DocumentApplicationService:
    """编排文档上传、查询和生命周期变更。"""

    _allowed_lifecycle_statuses: ClassVar[frozenset[str]] = frozenset(
        {"draft", "approved", "deprecated"}
    )

    def __init__(
        self,
        settings: Settings,
        storage: ObjectStorage,
        repository: DocumentRepository,
        ingestion: IngestionProcessor,
    ):
        self.settings = settings
        self.storage = storage
        self.repository = repository
        self.ingestion = ingestion

    def list_projects(self, db: Session) -> list[Project]:
        """返回可供前端选择的知识库项目。"""

        return self.repository.list_projects(db)

    def upload(self, db: Session, command: UploadDocumentCommand) -> UploadDocumentResult:
        """保存不可变原始文件，并创建或复用文档版本和入库任务。"""

        self._validate_upload(command)
        filename = Path(command.filename).name or "document"
        try:
            path, sha256, _ = self.storage.save(
                command.stream,
                filename,
                self.settings.max_upload_bytes,
            )
        except ValueError as exc:
            raise UploadTooLargeError(str(exc)) from exc

        project = self.repository.get_or_create_project(db, command.project.strip())
        document = self.repository.get_or_restore_document(
            db,
            project_id=project.id,
            logical_key=(command.logical_key or filename).strip(),
            filename=filename,
            document_type=command.document_type.strip(),
            owner=command.owner,
        )
        duplicate = self.repository.find_version_by_hash(
            db,
            document_id=document.id,
            sha256=sha256,
        )
        if duplicate is not None:
            return self._reuse_duplicate(db, document.id, duplicate)

        version = DocumentVersion(
            document_id=document.id,
            sha256=sha256,
            storage_path=str(path.relative_to(self.settings.storage_root)),
            lifecycle_status=command.lifecycle_status,
            technical_status="received",
            version_label=command.version_label,
            effective_at=command.effective_at,
            supersedes_document_id=command.supersedes_document_id,
        )
        db.add(version)
        db.flush()
        job = self.repository.create_job(db, document.id, version.id)
        self.repository.commit(db)

        inline_job_id = None
        if self.settings.run_inline_ingestion:
            # 生产环境由独立 Worker 领取 queued 任务；内联模式只服务本地开发。
            job.status = "running"
            self.repository.commit(db)
            inline_job_id = job.id
        return UploadDocumentResult(
            response=UploadResponse(
                document_id=document.id,
                version_id=version.id,
                job_id=job.id,
            ),
            inline_job_id=inline_job_id,
        )

    def _validate_upload(self, command: UploadDocumentCommand) -> None:
        """校验会影响文档业务语义的上传字段。"""

        if command.lifecycle_status not in self._allowed_lifecycle_statuses:
            raise InvalidRequestError(
                "lifecycle_status must be draft, approved, or deprecated"
            )
        suffix = Path(command.filename).suffix.lower()
        if suffix not in DocumentParser.allowed_suffixes:
            raise UnsupportedDocumentError(f"Unsupported file type: {suffix}")

    def _reuse_duplicate(
        self,
        db: Session,
        document_id: str,
        version: DocumentVersion,
    ) -> UploadDocumentResult:
        """复用相同内容版本；必要时创建新的入库尝试。"""

        job = self.repository.latest_job_for_version(db, version.id)
        needs_retry = (
            job is None
            or job.status == "failed"
            or version.technical_status in {"deleted", "failed_final"}
        )
        if needs_retry:
            job = self.repository.create_job(db, document_id, version.id)
            self.repository.commit(db)
        assert job is not None
        return UploadDocumentResult(
            response=UploadResponse(
                document_id=document_id,
                version_id=version.id,
                job_id=job.id,
                duplicate=True,
            )
        )

    def process_ingestion_job(self, db: Session, job_id: str) -> None:
        """供开发模式后台任务调用真实入库处理器。"""

        self.ingestion.process(db, job_id)

    def get_job(self, db: Session, job_id: str) -> IngestionJob:
        """查询入库任务，不存在时返回统一应用异常。"""

        job = self.repository.get_job(db, job_id)
        if job is None:
            raise ResourceNotFoundError("Ingestion job not found")
        return job

    def retry_job(self, db: Session, job_id: str) -> IngestionJob:
        """为已结束任务创建新的入库尝试，不覆盖历史记录。"""

        job = self.get_job(db, job_id)
        if job.status not in {"failed", "succeeded"}:
            raise ResourceConflictError("Only completed jobs can be re-queued")
        retry = self.repository.create_job(db, job.document_id, job.version_id)
        self.repository.commit(db)
        return retry

    def list_versions(self, db: Session, document_id: str) -> list[DocumentVersion]:
        """按创建时间倒序返回未删除文档的版本。"""

        versions = self.repository.list_versions(db, document_id)
        if versions is None:
            raise ResourceNotFoundError("Document not found")
        return versions

    def approve_version(self, db: Session, version_id: str) -> DocumentVersion:
        """请求批准已完成索引校验的版本。"""

        version = self._get_version(db, version_id)
        if version.technical_status != "searchable":
            raise ResourceConflictError(
                "Only a fully indexed and verified version can be approved"
            )
        # current 切换必须由 Index Worker 在投影校验通过后原子完成。
        version.lifecycle_status = "approved"
        version.is_current = False
        self.repository.add_outbox_event(
            db,
            version_id=version.id,
            event_type="index_version",
        )
        self.repository.commit(db)
        return version

    def deprecate_version(self, db: Session, version_id: str) -> DocumentVersion:
        """废弃版本并通过 Outbox 异步删除搜索投影。"""

        version = self._get_version(db, version_id)
        version.lifecycle_status = "deprecated"
        version.is_current = False
        version.effective_to = datetime.now(version.created_at.tzinfo)
        self.repository.add_outbox_event(
            db,
            version_id=version.id,
            event_type="delete_version",
        )
        self.repository.commit(db)
        return version

    def _get_version(self, db: Session, version_id: str) -> DocumentVersion:
        """读取版本并统一不存在错误。"""

        version = self.repository.get_version(db, version_id)
        if version is None:
            raise ResourceNotFoundError("Document version not found")
        return version

    def get_source(self, db: Session, chunk_id: str) -> SourceOut:
        """返回引用分块的连续原文和来源定位信息。"""

        chunk = self.repository.get_source_chunk(db, chunk_id)
        if chunk is None or chunk.version.document.is_deleted:
            raise ResourceNotFoundError("Source not found")
        version = chunk.version
        return SourceOut(
            chunk_id=chunk.id,
            document_id=version.document_id,
            version_id=version.id,
            filename=version.document.filename,
            content=chunk.content,
            heading_path=chunk.heading_path,
            page_number=chunk.page_number,
            sheet_name=chunk.sheet_name,
            cell_range=chunk.cell_range,
            lifecycle_status=version.lifecycle_status,
            version_label=version.version_label,
            effective_at=version.effective_at,
        )

    def download(
        self,
        db: Session,
        *,
        document_id: str,
        version_id: str | None,
    ) -> DownloadDocumentResult:
        """定位指定版本的不可变原始文件。"""

        version = self.repository.get_download_version(
            db,
            document_id=document_id,
            version_id=version_id,
        )
        if version is None or version.document.is_deleted:
            raise ResourceNotFoundError("Document not found")
        path = self.storage.resolve(version.storage_path)
        if not path.exists():
            raise ResourceNotFoundError("Stored file is missing")
        return DownloadDocumentResult(path=path, filename=version.document.filename)

    def delete_document(self, db: Session, document_id: str) -> None:
        """软删除文档，并为每个版本发布投影删除事件。"""

        document = self.repository.get_active_document_with_versions(db, document_id)
        if document is None:
            raise ResourceNotFoundError("Document not found")
        document.is_deleted = True
        for version in document.versions:
            version.is_current = False
            self.repository.add_outbox_event(
                db,
                version_id=version.id,
                event_type="delete_version",
            )
        self.repository.commit(db)
