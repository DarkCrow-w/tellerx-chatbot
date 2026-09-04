"""Offline-safe full-index rebuild and alias-switch command."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.container import model_client, search_index
from app.core.logging import configure_logging
from app.db import SessionLocal
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    IngestionJob,
)
from app.integrations.search import SearchIndex
from app.integrations.storage import LocalObjectStorage
from app.knowledge.chunking import TextChunk
from app.knowledge.parsers import DocumentParser
from app.services.indexing import IndexingService
from app.services.ingestion import IngestionService

logger = logging.getLogger(__name__)


def _eligible_versions(db: Session) -> list[DocumentVersion]:
    """返回当前应出现在搜索投影中的全部版本。"""

    return list(
        db.scalars(
            select(DocumentVersion)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(
                Document.is_deleted.is_(False),
                or_(
                    DocumentVersion.lifecycle_status == "draft",
                    (
                        (DocumentVersion.lifecycle_status == "approved")
                        & (DocumentVersion.is_current.is_(True))
                    ),
                ),
                DocumentVersion.technical_status == "searchable",
            )
            .order_by(DocumentVersion.id)
        )
    )


def _missing_embedding_chunks(
    db: Session,
    chunks: list[Chunk],
    fingerprint: str,
) -> list[Chunk]:
    """找出当前向量空间中没有缓存关系的分块。"""

    return [
        chunk
        for chunk in chunks
        if not db.scalar(
            select(ChunkEmbedding).where(
                ChunkEmbedding.chunk_id == chunk.id,
                ChunkEmbedding.embedding_fingerprint == fingerprint,
            )
        )
    ]


def _as_text_chunk(chunk: Chunk) -> TextChunk:
    """把持久化分块转换为向量服务使用的纯文本值对象。"""

    return TextChunk(
        ordinal=chunk.ordinal,
        content=chunk.content,
        content_hash=chunk.content_hash,
        token_count=chunk.token_count,
        heading_path=chunk.heading_path,
        page_number=chunk.page_number,
        sheet_name=chunk.sheet_name,
        cell_range=chunk.cell_range,
    )


def _ensure_embeddings(
    db: Session,
    chunks: list[Chunk],
    settings: Settings,
    ingestion: IngestionService,
    *,
    bm25_only: bool,
) -> None:
    """刷新上下文化输入身份，并为缺失分块补齐当前向量空间关系。"""

    text_chunks = [_as_text_chunk(chunk) for chunk in chunks]
    document = chunks[0].version.document
    ingestion._prepare_embedding_inputs(text_chunks, document)
    text_by_chunk_id = {
        chunk.id: text_chunk for chunk, text_chunk in zip(chunks, text_chunks)
    }
    for chunk, text_chunk in zip(chunks, text_chunks):
        chunk.embedding_input_hash = text_chunk.embedding_input_hash or chunk.content_hash
    if bm25_only:
        db.commit()
        return
    missing = _missing_embedding_chunks(db, chunks, settings.embedding_fingerprint)
    if not missing:
        db.commit()
        return
    missing_text_chunks = [text_by_chunk_id[chunk.id] for chunk in missing]
    cache_by_hash = ingestion._embeddings(
        db, missing_text_chunks, [], version_id=missing[0].version_id
    )
    for chunk, text_chunk in zip(missing, missing_text_chunks):
        cached = cache_by_hash.get(text_chunk.embedding_input_hash)
        if cached is not None:
            db.add(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    embedding_fingerprint=settings.embedding_fingerprint,
                    cache_id=cached.id,
                )
            )
    db.commit()


def _index_version(
    db: Session,
    version: DocumentVersion,
    *,
    settings: Settings,
    ingestion: IngestionService,
    indexer: IndexingService,
    index: SearchIndex,
    bm25_only: bool,
) -> int:
    """补齐向量、写入一个版本的投影并立即校验数量。"""

    chunks = list(
        db.scalars(select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal))
    )
    if not chunks:
        return 0
    _ensure_embeddings(db, chunks, settings, ingestion, bm25_only=bm25_only)
    _, documents = indexer._documents_for_version(db, version.id)
    if bm25_only:
        for document in documents:
            document.pop("embedding", None)
    index.index_chunks(documents)
    actual = index.count_version(version.id)
    if actual != len(chunks):
        raise RuntimeError(
            f"Version verification failed: {version.id}, "
            f"expected={len(chunks)}, actual={actual}"
        )
    return len(chunks)


def _rebuild(
    db: Session,
    *,
    settings: Settings,
    ingestion: IngestionService,
    indexer: IndexingService,
    index: SearchIndex,
    bm25_only: bool,
) -> tuple[int, int]:
    """重建全部合格版本，并完成全局数量和清单核验。"""

    expected_total = sum(
        _index_version(
            db,
            version,
            settings=settings,
            ingestion=ingestion,
            indexer=indexer,
            index=index,
            bm25_only=bm25_only,
        )
        for version in _eligible_versions(db)
    )
    pruned = index.prune_ineligible()
    actual_total = index.count_all()
    if actual_total != expected_total:
        raise RuntimeError(
            "PostgreSQL search verification failed: "
            f"expected={expected_total}, actual={actual_total}"
        )
    reconciliation = indexer.reconcile(db, repair=False)
    if reconciliation["difference_count"]:
        raise RuntimeError("PostgreSQL search manifest reconciliation failed")
    return expected_total, pruned


def _reparse_versions(
    db: Session,
    *,
    ingestion: IngestionService,
    indexer: IndexingService,
) -> int:
    """通过正式入库链路重建章节、分块和上下文化向量。"""

    versions = _eligible_versions(db)
    completed = 0
    for version in versions:
        job = IngestionJob(
            document_id=version.document_id,
            version_id=version.id,
            status="running",
            stage="starting",
        )
        db.add(job)
        db.commit()
        event_id = ingestion.process(db, job.id)
        if event_id is not None:
            indexer.publish_event(db, event_id)
            completed += 1
    return completed


def main() -> None:
    """从数据库事实表离线重建全部词法与向量搜索投影。"""

    parser = argparse.ArgumentParser(
        description="Rebuild PostgreSQL full-text and pgvector search rows"
    )
    parser.add_argument("--bm25-only", action="store_true")
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="rebuild section trees and contextual chunks from immutable source files",
    )
    args = parser.parse_args()

    settings = get_settings()
    # 机器可读的最终 JSON 保留在 stdout，过程日志写 stderr。
    configure_logging(settings.log_level, stream=sys.stderr)
    logger.info("全量索引重建开始 bm25_only=%s", args.bm25_only)
    index = search_index()
    index.ensure_index()
    started = time.perf_counter()
    storage = LocalObjectStorage(settings.storage_root)
    indexer = IndexingService(settings, index, storage)
    ingestion = IngestionService(
        settings,
        DocumentParser(settings.parser_backend),
        model_client(),
        index,
        indexer,
        storage,
    )
    with SessionLocal() as db:
        try:
            reparsed = (
                _reparse_versions(db, ingestion=ingestion, indexer=indexer)
                if args.reparse
                else 0
            )
            expected_total, pruned = _rebuild(
                db,
                settings=settings,
                ingestion=ingestion,
                indexer=indexer,
                index=index,
                bm25_only=args.bm25_only,
            )
        except Exception:
            logger.exception("全量索引重建失败")
            raise

    logger.info(
        "全量索引重建完成 chunks=%d pruned_rows=%d elapsed_seconds=%.3f",
        expected_total,
        pruned,
        time.perf_counter() - started,
    )

    print(
        json.dumps(
            {
                "status": "succeeded",
                "backend": settings.search_backend,
                "model": settings.embedding_model,
                "embedding_fingerprint": settings.embedding_fingerprint,
                "dimensions": settings.embedding_dimensions,
                "chunks": expected_total,
                "pruned_rows": pruned,
                "index": index.trace_index_name(),
                "bm25_only": args.bm25_only,
                "reparsed_versions": reparsed,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
