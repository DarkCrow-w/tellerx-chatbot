"""Typed request and response contracts shared across application boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LifecycleStatus = Literal["draft", "approved", "deprecated"]
AnswerStatus = Literal["answered", "insufficient_evidence", "conflict"]


class ProjectOut(BaseModel):
    """知识库项目的公开摘要。"""

    id: str
    name: str

    model_config = {"from_attributes": True}


class ProjectNameIn(BaseModel):
    """创建或重命名知识库时使用的名称。"""

    name: str = Field(min_length=1, max_length=200)


class DocumentCapabilitiesOut(BaseModel):
    """前端上传校验所需的服务端能力，避免两端重复硬编码。"""

    allowed_extensions: list[str]
    max_upload_bytes: int
    default_lifecycle_status: LifecycleStatus = "approved"


class UploadResponse(BaseModel):
    """文档上传受理结果及对应异步任务标识。"""

    document_id: str
    version_id: str
    job_id: str
    duplicate: bool = False


class JobOut(BaseModel):
    """文档入库任务的进度和诊断信息。"""

    id: str
    document_id: str
    version_id: str
    status: str
    stage: str
    progress: int
    error_message: str | None = None
    warnings: list = Field(default_factory=list)

    model_config = {"from_attributes": True}


class VersionOut(BaseModel):
    """文档版本的生命周期与技术处理状态。"""

    id: str
    document_id: str
    sha256: str
    lifecycle_status: str
    technical_status: str
    is_current: bool
    version_label: str | None = None
    effective_at: datetime | None = None
    effective_to: datetime | None = None
    indexed_at: datetime | None = None
    searchable_at: datetime | None = None
    parse_warnings: list = Field(default_factory=list)

    model_config = {"from_attributes": True}


class DocumentSummaryOut(BaseModel):
    """知识库管理页使用的文档、版本和任务聚合摘要。"""

    id: str
    project_id: str
    logical_key: str
    filename: str
    document_type: str
    owner: str | None = None
    created_at: datetime
    updated_at: datetime
    version_count: int
    current_version: VersionOut | None = None
    latest_version: VersionOut | None = None
    latest_job: JobOut | None = None


class DocumentPageOut(BaseModel):
    """可搜索、可分页的文档目录。"""

    items: list[DocumentSummaryOut]
    total: int
    limit: int
    offset: int


class BulkDeleteDocumentsIn(BaseModel):
    """限定在一个知识库内批量软删除的文档 ID。"""

    document_ids: list[str] = Field(min_length=1, max_length=100)


class BulkDeleteDocumentsOut(BaseModel):
    """批量删除结果；过期或越界 ID 会被安全跳过。"""

    requested_count: int
    deleted_count: int
    skipped_count: int
    deleted_ids: list[str]
    skipped_ids: list[str]


class ProjectCleanupOut(BaseModel):
    """清空或删除知识库后的物理数据清理统计。"""

    project_id: str
    project_deleted: bool
    documents_deleted: int
    versions_deleted: int
    chunks_deleted: int
    embedding_cache_deleted: int
    files_deleted: int
    files_failed: int


class IndexStatusOut(BaseModel):
    """搜索后端健康状态和一致性指标。"""

    available: bool
    backend: str | None = None
    server_version: str | None = None
    vector_version: str | None = None
    trgm_version: str | None = None
    table_ready: bool = False
    physical_index: str | None = None
    indexed_chunks: int = 0
    embedding_fingerprint: str | None = None
    missing_embeddings: int = 0
    pending_events: int = 0
    dead_events: int = 0
    sync_differences: int = 0


class ChatRequest(BaseModel):
    """知识库问答请求及可选的会话、检索范围和固定模型。"""

    question: str = Field(min_length=2, max_length=4000)
    conversation_id: str | None = None
    project_ids: list[str] = Field(default_factory=list)
    pinned_model: str | None = None


class CitationOut(BaseModel):
    """可定位到原文的精确引用。"""

    chunk_id: str
    document_id: str
    filename: str
    document_status: str
    heading_path: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    quote: str


class ClaimOut(BaseModel):
    """回答中的单项事实声明及其引用分块。"""

    text: str
    citations: list[str]


class ChatResponse(BaseModel):
    """经过证据校验的回答、声明、来源和追踪标识。"""

    status: AnswerStatus
    answer: str
    claims: list[ClaimOut]
    sources: list[CitationOut]
    model_id: str | None = None
    route_tier: str | None = None
    conversation_id: str
    message_id: str
    trace_id: str


class FeedbackIn(BaseModel):
    """对已保存助手消息的结构化反馈。"""

    message_id: str
    rating: Literal["correct", "incorrect", "missing_source", "irrelevant_source"]
    comment: str | None = Field(default=None, max_length=2000)


class UsageOut(BaseModel):
    """单个注册模型的本地配额用量概览。"""

    model_id: str
    tier: str
    quota_tokens: int
    used_tokens: int
    remaining_tokens: int
    usage_ratio: float
    enabled: bool


class SourceOut(BaseModel):
    """引用分块的完整内容、版本和来源位置。"""

    chunk_id: str
    document_id: str
    version_id: str
    filename: str
    content: str
    heading_path: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    lifecycle_status: str
    version_label: str | None = None
    effective_at: datetime | None = None
