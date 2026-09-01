"""Environment configuration and startup-time safety validation."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量和 Secret 文件加载并校验的进程配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "TellerX Knowledge Chatbot"
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://tellerx:tellerx@127.0.0.1:5432/tellerx"
    search_backend: str = "postgresql-pgvector-fts"
    postgres_search_table: str = "chunk_search_index"
    postgres_search_schema_version: int = 1
    pgvector_hnsw_ef_search: int = 200
    storage_root: Path = Path(".local-data/knowledge")

    # 内部和本地模型服务都通过 OpenAI 兼容 SDK 接入。旧 QWEN_* 名称保留为迁移别名，
    # 新环境只需要配置 MODEL_API_* / EMBEDDING_*。
    model_api_key_file: Path = Field(
        default=Path(".secrets/model_api_key.txt"),
        validation_alias=AliasChoices(
            "MODEL_API_KEY_FILE",
            "QWEN_API_KEY_FILE",
            "qwen_api_key_file",
        ),
    )
    model_api_base_url: str = Field(
        default="https://sdk-endpoint.example.internal/v1",
        validation_alias=AliasChoices(
            "MODEL_API_BASE_URL",
            "QWEN_CHAT_BASE_URL",
            "qwen_chat_base_url",
        ),
    )
    model_api_json_mode_enabled: bool = True
    # 内部 qwen3-embedding 必须实际返回 1024 维，否则需要新增数据库迁移。
    embedding_model: str = Field(
        default="qwen3-embedding",
        validation_alias=AliasChoices(
            "EMBEDDING_MODEL",
            "QWEN_EMBEDDING_MODEL",
            "qwen_embedding_model",
        ),
    )
    embedding_dimensions: int = Field(
        default=1024,
        validation_alias=AliasChoices(
            "EMBEDDING_DIMENSIONS",
            "QWEN_EMBEDDING_DIMENSIONS",
            "qwen_embedding_dimensions",
        ),
    )
    embedding_preprocess_version: str = "normalized-text-v1"
    rerank_enabled: bool = False
    model_api_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices(
            "MODEL_API_TIMEOUT_SECONDS",
            "QWEN_TIMEOUT_SECONDS",
            "qwen_timeout_seconds",
        ),
    )
    model_api_max_retries: int = Field(
        default=2,
        validation_alias=AliasChoices(
            "MODEL_API_MAX_RETRIES",
            "QWEN_MAX_RETRIES",
            "qwen_max_retries",
        ),
    )

    model_registry_path: Path = Path("config/models.yaml")
    allow_bm25_only: bool = True
    # 本地核心版默认在 API 的后台任务中完成解析、向量化和索引发布，不再要求
    # 额外启动 ingestion worker 和 indexer 两个常驻进程。
    run_inline_ingestion: bool = True
    parser_backend: str = "native"
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
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    @model_validator(mode="after")
    def validate_runtime_invariants(self) -> Settings:
        """拒绝不安全、相互矛盾或与数据库结构不兼容的配置。"""

        self._validate_search_configuration()
        self._validate_chunk_configuration()
        self._validate_retrieval_configuration()
        self._validate_embedding_configuration()
        self._validate_production_configuration()
        return self

    def _validate_search_configuration(self) -> None:
        """保证运行配置与当前 PostgreSQL 搜索迁移兼容。"""

        if self.search_backend != "postgresql-pgvector-fts":
            raise ValueError("SEARCH_BACKEND must be postgresql-pgvector-fts")
        if self.postgres_search_table != "chunk_search_index":
            raise ValueError("POSTGRES_SEARCH_TABLE is fixed to chunk_search_index")
        if self.database_url.startswith("postgresql") and self.embedding_dimensions != 1024:
            raise ValueError(
                "EMBEDDING_DIMENSIONS must be 1024 for migration 0004; "
                "a dimension change requires a new PostgreSQL vector schema migration"
            )

    def _validate_chunk_configuration(self) -> None:
        """校验切块目标、上限和重叠窗口的大小关系。"""

        if not 0 <= self.chunk_overlap_tokens < self.chunk_target_tokens <= self.chunk_max_tokens:
            raise ValueError("chunk sizes must satisfy 0 <= overlap < target <= maximum")

    def _validate_retrieval_configuration(self) -> None:
        """校验召回、重排和最终证据数量的漏斗关系。"""

        if not 1 <= self.evidence_top_k <= self.rerank_candidates <= self.retrieval_top_k:
            raise ValueError("retrieval sizes must satisfy evidence <= rerank <= retrieval")

    def _validate_embedding_configuration(self) -> None:
        """校验向量、重试和查询缓存参数。"""

        if self.embedding_dimensions <= 0 or self.pgvector_hnsw_ef_search <= 0:
            raise ValueError("embedding dimensions and HNSW ef_search must be positive")
        if self.model_api_max_retries < 0:
            raise ValueError("model_api_max_retries cannot be negative")
        if self.query_embedding_cache_size <= 0 or self.query_embedding_cache_ttl_seconds <= 0:
            raise ValueError("query embedding cache size and TTL must be positive")

    def _validate_production_configuration(self) -> None:
        """在生产模式拒绝弱一致性或过宽暴露配置。"""

        if self.app_env.casefold() in {"prod", "production"}:
            if self.allow_bm25_only:
                raise ValueError("production requires ALLOW_BM25_ONLY=false")
            if self.run_inline_ingestion:
                raise ValueError("production requires asynchronous ingestion workers")
            if "*" in self.cors_origins:
                raise ValueError("production CORS origins cannot contain a wildcard")

    @property
    def model_api_key(self) -> str:
        """按需读取模型网关密钥，避免把 Secret 内容常驻配置序列化结果。"""

        try:
            value = self.model_api_key_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Model API key file is missing: {self.model_api_key_file}"
            ) from exc
        if not value:
            raise RuntimeError("Model API key file is empty")
        return value

    @property
    def embedding_fingerprint(self) -> str:
        """为模型、维度和预处理版本生成稳定向量空间指纹。"""

        payload = ":".join(
            [
                self.embedding_model,
                str(self.embedding_dimensions),
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
