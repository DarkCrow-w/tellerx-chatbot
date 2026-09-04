"""文档目录、上传和生命周期应用用例。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, ClassVar, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.errors import (
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
    UnsupportedDocumentError,
    UploadTooLargeError,
)
from app.contracts.schemas import (
    BulkDeleteDocumentsOut,
    DocumentCandidateOut,
    DocumentCapabilitiesOut,
    DocumentPageOut,
    DocumentSectionOut,
    DocumentSummaryOut,
    JobOut,
    ProjectCleanupOut,
    SectionContextOut,
    SourceOut,
    UploadResponse,
    VersionOut,
)
from app.core.config import Settings
from app.db.models import DocumentVersion, IngestionJob, Project
from app.knowledge.document_scope import has_meaningful_document_hint
from app.knowledge.parsers import DocumentParser
from app.repositories.documents import DocumentRepository

logger = logging.getLogger(__name__)


class ObjectStorage(Protocol):
    """文档应用层需要的最小对象存储能力。"""

    def save(
        self, stream: BinaryIO, filename: str, max_bytes: int
    ) -> tuple[Path, str, int]: ...

    def resolve(self, storage_path: str) -> Path: ...

    def delete(self, storage_path: str) -> bool: ...


class IngestionProcessor(Protocol):
    """后台入库处理器接口。"""

    def process(self, db: Session, job_id: str) -> str | None: ...


class IndexPublisher(Protocol):
    """把入库产生的 Outbox 事件发布到 PostgreSQL 搜索投影。"""

    def publish_event(self, db: Session, event_id: str) -> bool: ...


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
        index_publisher: IndexPublisher | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.repository = repository
        self.ingestion = ingestion
        self.index_publisher = index_publisher

    def list_projects(self, db: Session) -> list[Project]:
        """返回可供前端选择的知识库项目。"""

        return self.repository.list_projects(db)

    def create_project(self, db: Session, name: str) -> Project:
        """创建空知识库，方便用户先组织目录再上传文档。"""

        normalized = name.strip()
        if not normalized:
            raise InvalidRequestError("Project name cannot be empty")
        if self.repository.get_project_by_name(db, normalized) is not None:
            raise ResourceConflictError("A project with this name already exists")
        try:
            project = self.repository.create_project(db, normalized)
            logger.info("知识库创建完成 project_id=%s name=%s", project.id, project.name)
            return project
        except IntegrityError as exc:
            db.rollback()
            if self.repository.get_project_by_name(db, normalized) is not None:
                raise ResourceConflictError("A project with this name already exists") from exc
            raise

    def rename_project(self, db: Session, project_id: str, name: str) -> Project:
        """重命名知识库，同时保持问答范围使用的项目 ID 不变。"""

        project = self.repository.get_project(db, project_id)
        if project is None:
            raise ResourceNotFoundError("Project not found")
        normalized = name.strip()
        if not normalized:
            raise InvalidRequestError("Project name cannot be empty")
        duplicate = self.repository.get_project_by_name(db, normalized)
        if duplicate is not None and duplicate.id != project.id:
            raise ResourceConflictError("A project with this name already exists")
        try:
            renamed = self.repository.rename_project(db, project, normalized)
            logger.info("知识库重命名完成 project_id=%s name=%s", renamed.id, renamed.name)
            return renamed
        except IntegrityError as exc:
            db.rollback()
            duplicate = self.repository.get_project_by_name(db, normalized)
            if duplicate is not None and duplicate.id != project_id:
                raise ResourceConflictError("A project with this name already exists") from exc
            raise

    def document_capabilities(self) -> DocumentCapabilitiesOut:
        """公开真实上传约束，供浏览器在传输前完成友好校验。"""

        return DocumentCapabilitiesOut(
            allowed_extensions=sorted(DocumentParser.allowed_suffixes),
            max_upload_bytes=self.settings.max_upload_bytes,
        )

    def list_documents(
        self,
        db: Session,
        *,
        project_id: str,
        query: str | None,
        limit: int,
        offset: int,
    ) -> DocumentPageOut:
        """聚合文档、版本和最近任务，返回管理页面所需的单次快照。"""

        if self.repository.get_project(db, project_id) is None:
            raise ResourceNotFoundError("Project not found")
        documents, total = self.repository.list_documents(
            db,
            project_id=project_id,
            query=query.strip() if query and query.strip() else None,
            limit=limit,
            offset=offset,
        )
        latest_versions = []
        sorted_versions: dict[str, list[DocumentVersion]] = {}
        for document in documents:
            # 同一事务内多个版本可能得到完全相同的微秒时间戳；ID 作为第二排序键，
            # 为“最新版本”建立稳定的全序，避免刷新后版本随机切换。
            versions = sorted(
                document.versions,
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
            sorted_versions[document.id] = versions
            if versions:
                latest_versions.append(versions[0].id)
        jobs = self.repository.latest_jobs_for_versions(db, latest_versions)
        items = []
        for document in documents:
            versions = sorted_versions[document.id]
            latest = versions[0] if versions else None
            current = next((item for item in versions if item.is_current), None)
            items.append(
                DocumentSummaryOut(
                    id=document.id,
                    project_id=document.project_id,
                    logical_key=document.logical_key,
                    filename=document.filename,
                    document_type=document.document_type,
                    owner=document.owner,
                    created_at=document.created_at,
                    updated_at=document.updated_at,
                    version_count=len(versions),
                    current_version=(VersionOut.model_validate(current) if current else None),
                    latest_version=(VersionOut.model_validate(latest) if latest else None),
                    latest_job=(
                        JobOut.model_validate(jobs[latest.id])
                        if latest is not None and latest.id in jobs
                        else None
                    ),
                )
            )
        return DocumentPageOut(items=items, total=total, limit=limit, offset=offset)

    def search_document_candidates(
        self, db: Session, *, project_id: str, hint: str, limit: int
    ) -> list[DocumentCandidateOut]:
        """返回可解释的文件名片段候选，不读取正文参与判断。"""

        if self.repository.get_project(db, project_id) is None:
            raise ResourceNotFoundError("Project not found")
        if not has_meaningful_document_hint(hint):
            raise InvalidRequestError("Document hint is too generic")
        return [
            DocumentCandidateOut.model_validate(item)
            for item in self.repository.search_document_candidates(
                db, project_id=project_id, hint=hint.strip(), limit=limit
            )
        ]

    def document_outline(
        self, db: Session, document_id: str
    ) -> list[DocumentSectionOut]:
        """读取文档当前有效版本的完整有序章节目录。"""

        document, sections = self.repository.get_current_outline(
            db, document_id=document_id
        )
        if document is None:
            raise ResourceNotFoundError("Document not found")
        return [DocumentSectionOut.model_validate(item, from_attributes=True) for item in sections]

    def section_context(self, db: Session, section_id: str) -> SectionContextOut:
        """返回章节原文和前后相邻块，供来源卡片按需展开。"""

        section, chunks, truncated = self.repository.get_section_context(
            db, section_id=section_id
        )
        if section is None:
            raise ResourceNotFoundError("Section not found")
        return SectionContextOut(
            section=DocumentSectionOut.model_validate(section, from_attributes=True),
            filename=section.version.document.filename,
            chunks=[self._source_out(chunk) for chunk in chunks],
            truncated=truncated,
        )

    def upload(self, db: Session, command: UploadDocumentCommand) -> UploadDocumentResult:
        """保存不可变原始文件，并创建或复用文档版本和入库任务。"""

        self._validate_upload(command)
        filename = Path(command.filename).name or "document"
        try:
            path, sha256, byte_size = self.storage.save(
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
            logger.info(
                "检测到重复文档内容 document_id=%s version_id=%s filename=%s",
                document.id,
                duplicate.id,
                filename,
            )
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
        logger.info(
            "文档上传已接收 project_id=%s document_id=%s version_id=%s job_id=%s "
            "filename=%s bytes=%d lifecycle=%s",
            project.id,
            document.id,
            version.id,
            job.id,
            filename,
            byte_size,
            command.lifecycle_status,
        )
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
        inline_job_id = None
        if needs_retry:
            job = self.repository.create_job(db, document_id, version.id)
            if self.settings.run_inline_ingestion:
                job.status = "running"
                inline_job_id = job.id
            self.repository.commit(db)
        assert job is not None
        return UploadDocumentResult(
            response=UploadResponse(
                document_id=document_id,
                version_id=version.id,
                job_id=job.id,
                duplicate=True,
            ),
            inline_job_id=inline_job_id,
        )

    def process_ingestion_job(self, db: Session, job_id: str) -> None:
        """在本地开发进程中依次完成入库和搜索投影发布。"""

        event_id = self.ingestion.process(db, job_id)
        if event_id is not None:
            self._publish_local_events(db, [event_id])

    def _publish_local_events(self, db: Session, event_ids: list[str]) -> None:
        """本地没有独立 Indexer，因此同步发布并有限重试 Outbox 事件。"""

        if not self.settings.run_inline_ingestion or self.index_publisher is None:
            return
        for event_id in event_ids:
            # 最终状态由 IndexingService 持久化，避免后台任务悄悄丢失索引事件。
            for _ in range(5):
                if self.index_publisher.publish_event(db, event_id):
                    break
            else:
                raise RuntimeError(f"Could not publish local index event: {event_id}")

    def get_job(self, db: Session, job_id: str) -> IngestionJob:
        """查询入库任务，不存在时返回统一应用异常。"""

        job = self.repository.get_job(db, job_id)
        if job is None:
            raise ResourceNotFoundError("Ingestion job not found")
        return job

    def retry_job(self, db: Session, job_id: str) -> IngestionJob:
        """为已结束任务创建新的入库尝试，不覆盖历史记录。"""

        job = self.get_job(db, job_id)
        if self.repository.get_active_document_with_versions(db, job.document_id) is None:
            raise ResourceNotFoundError("Document not found")
        if job.status not in {"failed", "succeeded"}:
            raise ResourceConflictError("Only completed jobs can be re-queued")
        retry = self.repository.create_job(db, job.document_id, job.version_id)
        if self.settings.run_inline_ingestion:
            retry.status = "running"
        self.repository.commit(db)
        logger.info(
            "入库任务已重试 previous_job_id=%s retry_job_id=%s document_id=%s",
            job_id,
            retry.id,
            retry.document_id,
        )
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
        # current 切换必须由搜索投影发布器在完整性校验通过后原子完成。
        version.lifecycle_status = "approved"
        version.is_current = False
        event = self.repository.add_outbox_event(
            db,
            version_id=version.id,
            event_type="index_version",
        )
        self.repository.commit(db)
        self._publish_local_events(db, [event.id])
        logger.info("文档版本已批准 version_id=%s event_id=%s", version.id, event.id)
        return version

    def deprecate_version(self, db: Session, version_id: str) -> DocumentVersion:
        """废弃版本并通过 Outbox 删除搜索投影。"""

        version = self._get_version(db, version_id)
        version.lifecycle_status = "deprecated"
        version.is_current = False
        version.effective_to = datetime.now(version.created_at.tzinfo)
        event = self.repository.add_outbox_event(
            db,
            version_id=version.id,
            event_type="delete_version",
        )
        self.repository.commit(db)
        self._publish_local_events(db, [event.id])
        logger.info("文档版本已废弃 version_id=%s event_id=%s", version.id, event.id)
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
        if chunk is None:
            raise ResourceNotFoundError("Source not found")
        version = chunk.version
        document = version.document
        if (
            document.is_deleted
            or document.visibility != "public"
            or version.lifecycle_status != "approved"
            or not version.is_current
            or version.technical_status != "searchable"
        ):
            raise ResourceNotFoundError("Source not found")
        return self._source_out(chunk)

    @staticmethod
    def _source_out(chunk) -> SourceOut:
        """把已加载的分块转换为包含章节定位的公开来源。"""

        version = chunk.version
        return SourceOut(
            chunk_id=chunk.id,
            document_id=version.document_id,
            version_id=version.id,
            filename=version.document.filename,
            content=chunk.content,
            heading_path=chunk.heading_path,
            section_id=chunk.section_id,
            breadcrumb=(
                [part.strip() for part in chunk.heading_path.split(">") if part.strip()]
                if chunk.heading_path
                else []
            ),
            section_level=chunk.section.level if chunk.section else None,
            location_confidence=(
                1.0 if chunk.section is not None and chunk.section.level > 0 else 0.7
            ),
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
        events = []
        for version in document.versions:
            version.is_current = False
            version.technical_status = "deleted"
            event = self.repository.add_outbox_event(
                db,
                version_id=version.id,
                event_type="delete_version",
            )
            events.append(event)
        version_ids = [version.id for version in document.versions]
        self.repository.cancel_active_jobs(db, [document.id])
        self.repository.supersede_pending_index_events(db, version_ids)
        self.repository.commit(db)
        self._publish_local_events(db, [event.id for event in events])
        logger.info(
            "文档已软删除 document_id=%s versions=%d events=%d",
            document_id,
            len(document.versions),
            len(events),
        )

    def bulk_delete_documents(
        self,
        db: Session,
        *,
        project_id: str,
        document_ids: list[str],
    ) -> BulkDeleteDocumentsOut:
        """在一个事务中软删除当前知识库的多份文档。"""

        if self.repository.get_project(db, project_id) is None:
            raise ResourceNotFoundError("Project not found")
        # 保持前端勾选顺序，同时避免重复 ID 产生重复 Outbox 事件。
        requested_ids = list(dict.fromkeys(document_ids))
        documents = self.repository.get_active_documents_with_versions(
            db,
            project_id=project_id,
            document_ids=requested_ids,
        )
        by_id = {document.id: document for document in documents}
        deleted_ids = [document_id for document_id in requested_ids if document_id in by_id]
        skipped_ids = [document_id for document_id in requested_ids if document_id not in by_id]
        events = []
        for document_id in deleted_ids:
            document = by_id[document_id]
            document.is_deleted = True
            for version in document.versions:
                version.is_current = False
                version.technical_status = "deleted"
                events.append(
                    self.repository.add_outbox_event(
                        db,
                        version_id=version.id,
                        event_type="delete_version",
                    )
                )
        version_ids = [
            version.id
            for document_id in deleted_ids
            for version in by_id[document_id].versions
        ]
        self.repository.cancel_active_jobs(db, deleted_ids)
        self.repository.supersede_pending_index_events(db, version_ids)
        self.repository.commit(db)
        self._publish_local_events(db, [event.id for event in events])
        logger.info(
            "文档批量软删除完成 project_id=%s requested=%d deleted=%d skipped=%d events=%d",
            project_id,
            len(requested_ids),
            len(deleted_ids),
            len(skipped_ids),
            len(events),
        )
        return BulkDeleteDocumentsOut(
            requested_count=len(requested_ids),
            deleted_count=len(deleted_ids),
            skipped_count=len(skipped_ids),
            deleted_ids=deleted_ids,
            skipped_ids=skipped_ids,
        )

    def cleanup_project(self, db: Session, project_id: str) -> ProjectCleanupOut:
        """物理回收已软删除文档的残留，不影响仍在使用的文档。"""

        return self._purge_project(
            db,
            project_id=project_id,
            delete_project=False,
            deleted_only=True,
        )

    def delete_project(self, db: Session, project_id: str) -> ProjectCleanupOut:
        """物理删除知识库以及全部文档、派生数据和无引用对象。"""

        return self._purge_project(db, project_id=project_id, delete_project=True)

    def _purge_project(
        self,
        db: Session,
        *,
        project_id: str,
        delete_project: bool,
        deleted_only: bool = False,
    ) -> ProjectCleanupOut:
        """提交数据库删除后回收文件；文件失败不回滚已经完成的数据清理。"""

        snapshot = self.repository.purge_project(
            db,
            project_id=project_id,
            delete_project=delete_project,
            deleted_only=deleted_only,
        )
        if snapshot is None:
            raise ResourceNotFoundError("Project not found")

        files_deleted = 0
        files_failed = 0
        for object_path in snapshot.object_paths:
            try:
                files_deleted += int(self.storage.delete(object_path))
            except (OSError, ValueError):
                files_failed += 1
                logger.exception(
                    "知识库对象删除失败 project_id=%s object_path=%s",
                    project_id,
                    object_path,
                )
        logger.info(
            "知识库物理清理完成 project_id=%s project_deleted=%s documents=%d "
            "versions=%d chunks=%d caches=%d files=%d failed_files=%d",
            project_id,
            delete_project,
            snapshot.documents_deleted,
            snapshot.versions_deleted,
            snapshot.chunks_deleted,
            snapshot.embedding_cache_deleted,
            files_deleted,
            files_failed,
        )
        return ProjectCleanupOut(
            project_id=project_id,
            project_deleted=delete_project,
            documents_deleted=snapshot.documents_deleted,
            versions_deleted=snapshot.versions_deleted,
            chunks_deleted=snapshot.chunks_deleted,
            embedding_cache_deleted=snapshot.embedding_cache_deleted,
            files_deleted=files_deleted,
            files_failed=files_failed,
        )
