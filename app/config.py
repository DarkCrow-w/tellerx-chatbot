from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "TellerX Knowledge Chatbot"
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://knowledge:knowledge@postgres:5432/knowledge"
    elasticsearch_url: str = "http://elasticsearch:9200"
    elasticsearch_read_alias: str = "knowledge-chunks-read"
    elasticsearch_write_alias: str = "knowledge-chunks-write"
    elasticsearch_index_prefix: str = "knowledge-chunks"
    elasticsearch_schema_version: int = 3
    elasticsearch_number_of_shards: int = 1
    elasticsearch_number_of_replicas: int = 0
    elasticsearch_verify_certs: bool = False
    elasticsearch_username: str | None = None
    elasticsearch_password_file: Path | None = None
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
    vector_num_candidates: int = 400
    vector_min_similarity: float | None = 0.25
    query_embedding_cache_size: int = 500
    query_embedding_cache_ttl_seconds: int = 3600
    prompt_version: str = "grounded-qa-v1"
    validate_citations_against_database: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])

    @property
    def qwen_api_key(self) -> str:
        try:
            value = self.qwen_api_key_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Qwen API key file is missing: {self.qwen_api_key_file}"
            ) from exc
        if not value:
            raise RuntimeError("Qwen API key file is empty")
        return value

    @property
    def elasticsearch_password(self) -> str | None:
        if not self.elasticsearch_password_file:
            return None
        try:
            value = self.elasticsearch_password_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Elasticsearch password file is missing: {self.elasticsearch_password_file}"
            ) from exc
        return value or None

    @property
    def embedding_fingerprint(self) -> str:
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
        return (
            f"{self.elasticsearch_index_prefix}-s{self.elasticsearch_schema_version}"
            f"-e{self.embedding_fingerprint}-000001"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
