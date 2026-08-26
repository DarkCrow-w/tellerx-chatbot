"""Environment configuration and startup-time safety validation."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量和 Secret 文件加载并校验的进程配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "TellerX Knowledge Chatbot"
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://knowledge:knowledge@postgres:5432/knowledge"
    search_backend: str = "postgresql-pgvector-fts"
    postgres_search_table: str = "chunk_search_index"
    postgres_search_schema_version: int = 1
    pgvector_hnsw_ef_search: int = 200
    storage_root: Path = Path("/data/knowledge")

    qwen_api_key_file: Path = Path("/run/secrets/qwen_api_key")
    qwen_chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_rerank_base_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1"
    # 百炼控制台名称：Qwen3.7-通用文本向量；API 模型 ID 如下。
    qwen_embedding_model: str = "qwen3.7-text-embedding"
    qwen_embedding_dimensions: int = 1024
    embedding_preprocess_version: str = "normalized-text-v1"
    qwen_rerank_model: str = "qwen3-rerank"
    qwen_timeout_seconds: float = 60.0
    qwen_max_retries: int = 2

    model_registry_path: Path = Path("config/models.yaml")
    allow_bm25_only: bool = True
    run_inline_ingestion: bool = False
    parser_backend: str = "native"
    worker_poll_seconds: float = 2.0
    index_reconcile_interval_seconds: int = 3600
    max_upload_bytes: int = 100 * 1024 * 1024
    chunk_target_tokens: int = 450
    chunk_max_tokens: int = 650
    chunk_overlap_tokens: int = 60
    retrieval_top_k: int = 50
    rerank_candidates: int = 30
    evidence_top_k: int = 8
    vector_min_similarity: float | None = 0.25
    query_embedding_cache_size: int = 500
    query_embedding_cache_ttl_seconds: int = 3600
    semantic_query_understanding_enabled: bool = True
    query_plan_cache_size: int = 500
    query_plan_cache_ttl_seconds: int = 3600
    prompt_version: str = "grounded-qa-v1"
    validate_citations_against_database: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])

    @model_validator(mode="after")
    def validate_runtime_invariants(self) -> Settings:
        """拒绝不安全、相互矛盾或与数据库结构不兼容的配置。"""

        if self.search_backend != "postgresql-pgvector-fts":
            raise ValueError("SEARCH_BACKEND must be postgresql-pgvector-fts")
        if self.postgres_search_table != "chunk_search_index":
            raise ValueError("POSTGRES_SEARCH_TABLE is fixed to chunk_search_index")
        if self.database_url.startswith("postgresql") and self.qwen_embedding_dimensions != 1024:
            raise ValueError(
                "QWEN_EMBEDDING_DIMENSIONS must be 1024 for migration 0004; "
                "a dimension change requires a new PostgreSQL vector schema migration"
            )
        if not 0 <= self.chunk_overlap_tokens < self.chunk_target_tokens <= self.chunk_max_tokens:
            raise ValueError("chunk sizes must satisfy 0 <= overlap < target <= maximum")
        if not 1 <= self.evidence_top_k <= self.rerank_candidates <= self.retrieval_top_k:
            raise ValueError("retrieval sizes must satisfy evidence <= rerank <= retrieval")
        if self.qwen_embedding_dimensions <= 0 or self.pgvector_hnsw_ef_search <= 0:
            raise ValueError("embedding dimensions and HNSW ef_search must be positive")
        if self.qwen_max_retries < 0:
            raise ValueError("qwen_max_retries cannot be negative")
        if self.query_embedding_cache_size <= 0 or self.query_embedding_cache_ttl_seconds <= 0:
            raise ValueError("query embedding cache size and TTL must be positive")

        if self.app_env.casefold() in {"prod", "production"}:
            if self.allow_bm25_only:
                raise ValueError("production requires ALLOW_BM25_ONLY=false")
            if self.run_inline_ingestion:
                raise ValueError("production requires asynchronous ingestion workers")
            if "*" in self.cors_origins:
                raise ValueError("production CORS origins cannot contain a wildcard")
        return self

    @property
    def qwen_api_key(self) -> str:
        """按需读取千问密钥，避免把 Secret 内容常驻配置序列化结果。"""

        try:
            value = self.qwen_api_key_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"Qwen API key file is missing: {self.qwen_api_key_file}") from exc
        if not value:
            raise RuntimeError("Qwen API key file is empty")
        return value

    @property
    def embedding_fingerprint(self) -> str:
        """为模型、维度和预处理版本生成稳定向量空间指纹。"""

        payload = ":".join(
            [
                self.qwen_embedding_model,
                str(self.qwen_embedding_dimensions),
                self.embedding_preprocess_version,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    @property
    def search_index_name(self) -> str:
        """生成用于查询追踪的可读搜索索引标识。"""

        return (
            f"postgresql-{self.postgres_search_table}-s{self.postgres_search_schema_version}"
            f"-e{self.embedding_fingerprint}-000001"
        )


@lru_cache
def get_settings() -> Settings:
    """返回当前进程缓存的已校验配置。"""

    return Settings()
