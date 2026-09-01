"""文档目录、版本和入库任务的数据访问。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.models import Chunk, Document, DocumentVersion, IngestionJob, OutboxEvent, Project


class DocumentRepository:
    """集中维护文档聚合相关的 SQLAlchemy 查询。"""

    def list_projects(self, db: Session) -> list[Project]:
        """按名称返回知识库项目。"""

        return list(db.scalars(select(Project).order_by(Project.name)))

    def get_or_create_project(self, db: Session, name: str) -> Project:
        """并发安全地取得或创建项目。"""

        project = db.scalar(select(Project).where(Project.name == name))
        if project is not None:
            return project
        try:
            # savepoint 只回滚竞争失败的 INSERT，不污染外层上传事务。
            with db.begin_nested():
                project = Project(name=name)
                db.add(project)
                db.flush()
        except IntegrityError:
            project = db.scalar(select(Project).where(Project.name == name))
            if project is None:
                raise
        return project

    def get_or_restore_document(
        self,
        db: Session,
        *,
        project_id: str,
        logical_key: str,
        filename: str,
        document_type: str,
        owner: str | None,
    ) -> Document:
        """取得稳定逻辑文档；不存在则创建，软删除则恢复。"""

        document = db.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.logical_key == logical_key,
            )
        )
        if document is None:
            document = Document(
                project_id=project_id,
                logical_key=logical_key,
                filename=filename,
                document_type=document_type,
                owner=owner,
            )
            db.add(document)
            db.flush()
            return document
        if document.is_deleted:
            # 软删除不会抹去逻辑身份；重新上传时恢复原聚合，避免同键歧义。
            document.is_deleted = False
            document.filename = filename
            document.document_type = document_type
            document.owner = owner
        return document

    def find_version_by_hash(
        self, db: Session, *, document_id: str, sha256: str
    ) -> DocumentVersion | None:
        """按文档身份和内容哈希查找不可变版本。"""

        return db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.sha256 == sha256,
            )
        )

    def latest_job_for_version(
        self, db: Session, version_id: str
    ) -> IngestionJob | None:
        """返回某版本最近一次入库尝试。"""

        return db.scalar(
            select(IngestionJob)
            .where(IngestionJob.version_id == version_id)
            .order_by(IngestionJob.created_at.desc())
        )

    def create_job(self, db: Session, document_id: str, version_id: str) -> IngestionJob:
        """创建新的、不可变的入库尝试记录。"""

        job = IngestionJob(document_id=document_id, version_id=version_id)
        db.add(job)
        return job

    def get_job(self, db: Session, job_id: str) -> IngestionJob | None:
        """按主键读取入库任务。"""

        return db.get(IngestionJob, job_id)

    def get_version(self, db: Session, version_id: str) -> DocumentVersion | None:
        """按主键读取文档版本。"""

        return db.get(DocumentVersion, version_id)

    def list_versions(self, db: Session, document_id: str) -> list[DocumentVersion] | None:
        """返回未删除文档的全部版本；文档不可见时返回 ``None``。"""

        document = db.get(Document, document_id)
        if document is None or document.is_deleted:
            return None
        return list(
            db.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(DocumentVersion.created_at.desc())
            )
        )

    def get_source_chunk(self, db: Session, chunk_id: str) -> Chunk | None:
        """连同版本和文档一次性加载引用分块。"""

        return db.scalar(
            select(Chunk)
            .options(joinedload(Chunk.version).joinedload(DocumentVersion.document))
            .where(Chunk.id == chunk_id)
        )

    def get_download_version(
        self, db: Session, *, document_id: str, version_id: str | None
    ) -> DocumentVersion | None:
        """读取指定版本；未指定时读取最近上传版本。"""

        statement = (
            select(DocumentVersion)
            .options(joinedload(DocumentVersion.document))
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.created_at.desc())
        )
        if version_id:
            statement = statement.where(DocumentVersion.id == version_id)
        return db.scalar(statement.limit(1))

    def get_active_document_with_versions(
        self, db: Session, document_id: str
    ) -> Document | None:
        """读取未删除文档及其全部版本。"""

        return db.scalar(
            select(Document)
            .options(joinedload(Document.versions))
            .where(Document.id == document_id, Document.is_deleted.is_(False))
        )

    @staticmethod
    def add_outbox_event(
        db: Session, *, version_id: str, event_type: str
    ) -> OutboxEvent:
        """把搜索投影变更意图加入当前数据库事务。"""

        event = OutboxEvent(aggregate_id=version_id, event_type=event_type, payload={})
        db.add(event)
        return event

    @staticmethod
    def commit(db: Session) -> None:
        """提交当前用例事务。"""

        db.commit()
