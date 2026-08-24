"""Operational visibility and explicitly scoped maintenance endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.schemas import IndexStatusOut, UsageOut
from app.core.config import get_settings
from app.core.container import indexing_service, model_router, search_index
from app.db import get_db
from app.db.models import Chunk, ChunkEmbedding, DocumentVersion, IndexSyncState, OutboxEvent

router = APIRouter(tags=["operations"])


@router.get("/index/status", response_model=IndexStatusOut)
def index_status(db: Session = Depends(get_db)) -> dict:
    """Combine PostgreSQL search health with durable indexing invariants."""

    settings = get_settings()
    state = search_index().status()
    eligible = (
        (DocumentVersion.lifecycle_status == "draft")
        | (
            (DocumentVersion.lifecycle_status == "approved")
            & (DocumentVersion.is_current.is_(True))
        )
    )
    has_current_embedding = (
        select(ChunkEmbedding.id)
        .where(
            ChunkEmbedding.chunk_id == Chunk.id,
            ChunkEmbedding.embedding_fingerprint == settings.embedding_fingerprint,
        )
        .exists()
    )
    state["embedding_fingerprint"] = settings.embedding_fingerprint
    state["missing_embeddings"] = int(
        db.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .where(
                DocumentVersion.technical_status == "searchable",
                eligible,
                ~has_current_embedding,
            )
        )
        or 0
    )
    state["pending_events"] = int(
        db.scalar(
            select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.status.in_(["pending", "processing"])
            )
        )
        or 0
    )
    state["dead_events"] = int(
        db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "dead")
        )
        or 0
    )
    state["sync_differences"] = int(
        db.scalar(
            select(func.count()).select_from(IndexSyncState).where(
                (IndexSyncState.status.in_(["pending", "mismatch", "failed"]))
                | (
                    (IndexSyncState.status == "verified")
                    & (IndexSyncState.expected_chunks != IndexSyncState.indexed_chunks)
                )
            )
        )
        or 0
    )
    return state


@router.post("/admin/indexes/reconcile")
def reconcile_index(repair: bool = False, db: Session = Depends(get_db)) -> dict:
    """Compare source tables with the PostgreSQL search projection and repair drift."""

    return indexing_service().reconcile(db, repair=repair)


@router.get("/models/usage", response_model=list[UsageOut])
def model_usage(db: Session = Depends(get_db)) -> list[dict]:
    """Return the local routing estimate; provider billing remains authoritative."""

    return model_router().usage_rows(db)


@router.post("/internal/diagnostics/qwen")
def diagnostics_notice() -> dict:
    """Prevent paid credential diagnostics from being exposed over HTTP."""

    return {
        "status": "disabled-over-http",
        "message": "Run `qwen-diagnostics` locally so credentials and paid diagnostics are not exposed via HTTP.",
    }
