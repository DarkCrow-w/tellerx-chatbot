"""SQLAlchemy persistence models and durable workflow records."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    """生成适合跨进程创建记录的 UUID 字符串主键。"""

    return str(uuid.uuid4())


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，供 ORM 默认值复用。"""

    return datetime.now(UTC)


class Project(Base):
    """知识库项目，是文档检索范围的顶层边界。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    documents: Mapped[list[Document]] = relationship(back_populates="project")


class Document(Base):
    """文档的稳定逻辑身份；同一文档的内容变化由版本表示。"""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_document_project_logical_key", "project_id", "logical_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    logical_key: Mapped[str] = mapped_column(String(500), nullable=False, default=new_id)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(50), default="upload")
    external_source_id: Mapped[str | None] = mapped_column(String(1000))
    visibility: Mapped[str] = mapped_column(String(30), default="public", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="documents")
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    """不可变文件版本及其业务生命周期、技术处理状态。"""

    __tablename__ = "document_versions"
    __table_args__ = (
        Index("ix_document_version_hash", "document_id", "sha256", unique=True),
        Index(
            "uq_document_current_approved",
            "document_id",
            unique=True,
            postgresql_where=text("is_current AND lifecycle_status = 'approved'"),
            sqlite_where=text("is_current = 1 AND lifecycle_status = 'approved'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    technical_status: Mapped[str] = mapped_column(String(40), default="received", index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    version_label: Mapped[str | None] = mapped_column(String(100))
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_document_id: Mapped[str | None] = mapped_column(String(36))
    supersedes_version_id: Mapped[str | None] = mapped_column(String(36))
    parser_fingerprint: Mapped[str | None] = mapped_column(String(200))
    chunker_fingerprint: Mapped[str | None] = mapped_column(String(200))
    parse_warnings: Mapped[list] = mapped_column(JSON, default=list)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    searchable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="versions")
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class Chunk(Base):
    """带稳定来源位置和相邻关系的可引用文本分块。"""

    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunk_version_ordinal", "version_id", "ordinal", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(String(1000))
    page_number: Mapped[int | None] = mapped_column(Integer)
    sheet_name: Mapped[str | None] = mapped_column(String(300))
    cell_range: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default=new_id)
    parent_chunk_id: Mapped[str | None] = mapped_column(String(36))
    previous_chunk_id: Mapped[str | None] = mapped_column(String(36))
    next_chunk_id: Mapped[str | None] = mapped_column(String(36))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    version: Mapped[DocumentVersion] = relationship(back_populates="chunks")


class IngestionJob(Base):
    """一次可审计、带租约的文档入库尝试。"""

    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(50), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    """问答消息的会话容器。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    """持久化的用户或助手消息及其回答元数据。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    answer_status: Mapped[str | None] = mapped_column(String(40))
    model_id: Mapped[str | None] = mapped_column(String(100))
    trace_id: Mapped[str | None] = mapped_column(String(36), index=True)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnswerFeedback(Base):
    """关联到助手消息的人工质量反馈。"""

    __tablename__ = "answer_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    rating: Mapped[str] = mapped_column(String(30), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelUsage(Base):
    """模型调用结果、Token 用量和延迟审计记录。"""

    __tablename__ = "model_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(String(100), index=True)
    request_id: Mapped[str] = mapped_column(String(100), index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result_status: Mapped[str] = mapped_column(String(40), nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(100))


class Principal(Base):
    """可被授予文档权限的用户或用户组身份。"""

    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)


class DocumentAcl(Base):
    """文档与主体之间的读取权限关联。"""

    __tablename__ = "document_acl"
    __table_args__ = (
        Index("ix_acl_document_principal", "document_id", "principal_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    permission: Mapped[str] = mapped_column(String(20), default="read")


class DocumentArtifact(Base):
    """由文档版本派生的不可变解析或处理中间产物。"""

    __tablename__ = "document_artifacts"
    __table_args__ = (
        Index(
            "ix_artifact_version_type_fingerprint",
            "version_id",
            "artifact_type",
            "fingerprint",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    object_uri: Mapped[str] = mapped_column(String(1200), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmbeddingModel(Base):
    """由指纹标识的向量空间元数据。"""

    __tablename__ = "embedding_models"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    similarity: Mapped[str] = mapped_column(String(30), default="cosine")
    preprocess_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmbeddingCache(Base):
    """按内容哈希和向量空间复用的不可变向量对象引用。"""

    __tablename__ = "embedding_cache"
    __table_args__ = (
        Index(
            "ix_embedding_content_fingerprint", "content_hash", "embedding_fingerprint", unique=True
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding_fingerprint: Mapped[str] = mapped_column(
        ForeignKey("embedding_models.fingerprint"), nullable=False, index=True
    )
    object_uri: Mapped[str] = mapped_column(String(1200), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChunkEmbedding(Base):
    """分块到指定向量空间缓存对象的关联。"""

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        Index("ix_chunk_embedding_unique", "chunk_id", "embedding_fingerprint", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), index=True)
    embedding_fingerprint: Mapped[str] = mapped_column(
        ForeignKey("embedding_models.fingerprint"), index=True
    )
    cache_id: Mapped[str] = mapped_column(ForeignKey("embedding_cache.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxEvent(Base):
    """保证数据库事实变更最终发布到搜索投影的事务事件。"""

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexGeneration(Base):
    """一个物理搜索结构代次及其整体同步统计。"""

    __tablename__ = "index_generations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    physical_index: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="building", index=True)
    expected_chunks: Mapped[int] = mapped_column(Integer, default=0)
    indexed_chunks: Mapped[int] = mapped_column(Integer, default=0)
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexSyncState(Base):
    """文档版本在特定索引代次中的清单校验状态。"""

    __tablename__ = "index_sync_state"
    __table_args__ = (
        Index("ix_sync_version_generation", "version_id", "generation_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    generation_id: Mapped[str] = mapped_column(ForeignKey("index_generations.id"), index=True)
    expected_chunks: Mapped[int] = mapped_column(Integer, default=0)
    indexed_chunks: Mapped[int] = mapped_column(Integer, default=0)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class QueryTrace(Base):
    """一次问答的检索计划、证据、模型和延迟追踪记录。"""

    __tablename__ = "query_traces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    normalized_query: Mapped[str] = mapped_column(Text, nullable=False)
    project_ids: Mapped[list] = mapped_column(JSON, default=list)
    index_name: Mapped[str] = mapped_column(String(500), nullable=False)
    retrieval_json: Mapped[dict] = mapped_column(JSON, default=dict)
    answer_status: Mapped[str | None] = mapped_column(String(40))
    model_id: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
