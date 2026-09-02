"""文档目录、版本和入库任务的数据访问。"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import (
    Chunk,
    Document,
    DocumentVersion,
    IngestionJob,
    OutboxEvent,
    Project,
    utcnow,
)


class DocumentRepository:
    """集中维护文档聚合相关的 SQLAlchemy 查询。"""

    def list_projects(self, db: Session) -> list[Project]:
        """按名称返回知识库项目。"""

        return list(db.scalars(select(Project).order_by(Project.name)))

    def get_project(self, db: Session, project_id: str) -> Project | None:
        """按主键读取知识库项目。"""

        return db.get(Project, project_id)

    def get_project_by_name(self, db: Session, name: str) -> Project | None:
        """按唯一名称读取知识库项目。"""

        return db.scalar(select(Project).where(Project.name == name))

    def create_project(self, db: Session, name: str) -> Project:
        """创建可在首次上传前存在的空知识库。"""

        project = Project(name=name)
        db.add(project)
        db.commit()
        return project

    def rename_project(self, db: Session, project: Project, name: str) -> Project:
        """修改知识库显示名称，不改变其稳定主键和文档归属。"""

        project.name = name
        db.commit()
        return project

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

    def list_documents(
        self,
        db: Session,
        *,
        project_id: str,
        query: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Document], int]:
        """分页返回项目文档，并预加载版本供应用层生成管理摘要。"""

        predicates = [
            Document.project_id == project_id,
            Document.is_deleted.is_(False),
        ]
        if query:
            pattern = f"%{query}%"
            predicates.append(
                or_(Document.filename.ilike(pattern), Document.logical_key.ilike(pattern))
            )
        total = db.scalar(select(func.count(Document.id)).where(*predicates)) or 0
        documents = list(
            db.scalars(
                select(Document)
                .options(selectinload(Document.versions))
                .where(*predicates)
                .order_by(Document.updated_at.desc(), Document.id)
                .offset(offset)
                .limit(limit)
            )
        )
        return documents, total

    def latest_jobs_for_versions(
        self, db: Session, version_ids: list[str]
    ) -> dict[str, IngestionJob]:
        """批量读取各版本最近一次任务，避免管理列表逐行查询。"""

        if not version_ids:
            return {}
        jobs = db.scalars(
            select(IngestionJob)
            .where(IngestionJob.version_id.in_(version_ids))
            .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
        )
        latest: dict[str, IngestionJob] = {}
        for job in jobs:
            latest.setdefault(job.version_id, job)
        return latest

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
        # 逻辑身份稳定，但目录摘要跟随最新上传更新；赋值也会刷新 updated_at。
        document.is_deleted = False
        document.filename = filename
        document.document_type = document_type
        document.owner = owner
        document.updated_at = utcnow()
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
            .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
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
                .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
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
            .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
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

    def get_active_documents_with_versions(
        self,
        db: Session,
        *,
        project_id: str,
        document_ids: list[str],
    ) -> list[Document]:
        """批量读取同一项目中仍可见的文档及版本，避免逐条查询。"""

        if not document_ids:
            return []
        return list(
            db.scalars(
                select(Document)
                .options(selectinload(Document.versions))
                .where(
                    Document.project_id == project_id,
                    Document.id.in_(document_ids),
                    Document.is_deleted.is_(False),
                )
            )
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
