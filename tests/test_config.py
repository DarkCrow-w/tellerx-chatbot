import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_rejects_inconsistent_chunk_and_retrieval_sizes() -> None:
    with pytest.raises(ValidationError, match="overlap < target"):
        Settings(_env_file=None, chunk_overlap_tokens=500, chunk_target_tokens=450)
    with pytest.raises(ValidationError, match="evidence <= rerank <= retrieval"):
        Settings(_env_file=None, evidence_top_k=31, rerank_candidates=30)


def test_production_disables_development_fallbacks() -> None:
    with pytest.raises(ValidationError, match="ALLOW_BM25_ONLY=false"):
        Settings(_env_file=None, app_env="production", allow_bm25_only=True)
    configured = Settings(
        _env_file=None,
        app_env="production",
        allow_bm25_only=False,
        run_inline_ingestion=False,
    )
    assert configured.app_env == "production"
