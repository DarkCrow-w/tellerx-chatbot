"""运维指标和数据库就绪状态的数据访问。"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Chunk, ChunkEmbedding, DocumentVersion, IndexSyncState, OutboxEvent


class OperationsRepository:
    """封装运行状态页面所需的聚合查询。"""

    def database_available(self, db: Session) -> bool:
        """执行最小查询判断当前数据库连接是否可用。"""

        try:
            db.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def index_counters(self, db: Session, embedding_fingerprint: str) -> dict[str, int]:
        """一次性计算索引状态接口需要的四类持久化计数。"""

        eligible = (DocumentVersion.lifecycle_status == "draft") | (
            (DocumentVersion.lifecycle_status == "approved")
            & (DocumentVersion.is_current.is_(True))
        )
        has_current_embedding = (
            select(ChunkEmbedding.id)
            .where(
                ChunkEmbedding.chunk_id == Chunk.id,
                ChunkEmbedding.embedding_fingerprint == embedding_fingerprint,
            )
            .exists()
        )
        missing_embeddings = db.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .where(
                DocumentVersion.technical_status == "searchable",
                eligible,
                ~has_current_embedding,
            )
        )
        pending_events = db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status.in_(["pending", "processing"]))
        )
        dead_events = db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "dead")
        )
        sync_differences = db.scalar(
            select(func.count())
            .select_from(IndexSyncState)
            .where(
                (IndexSyncState.status.in_(["pending", "mismatch", "failed"]))
                | (
                    (IndexSyncState.status == "verified")
                    & (IndexSyncState.expected_chunks != IndexSyncState.indexed_chunks)
                )
            )
        )
        return {
            "missing_embeddings": int(missing_embeddings or 0),
            "pending_events": int(pending_events or 0),
            "dead_events": int(dead_events or 0),
            "sync_differences": int(sync_differences or 0),
        }
