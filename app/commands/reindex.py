"""Offline-safe full-index rebuild and alias-switch command."""

from __future__ import annotations

import argparse
import json
import logging
import time

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.container import qwen_client, search_index
from app.db import SessionLocal
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    DocumentVersion,
)
from app.integrations.storage import LocalObjectStorage
from app.knowledge.chunking import TextChunk
from app.knowledge.parsers import DocumentParser
from app.services.indexing import IndexingService
from app.services.ingestion import IngestionService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild PostgreSQL full-text and pgvector search rows"
    )
    parser.add_argument("--bm25-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    index = search_index()
    index.ensure_index()
    started = time.perf_counter()
    storage = LocalObjectStorage(settings.storage_root)
    indexer = IndexingService(settings, index, storage)
    ingestion = IngestionService(
        settings,
        DocumentParser(settings.parser_backend),
        qwen_client(),
        index,
        indexer,
        storage,
    )

    with SessionLocal() as db:
        versions = list(
            db.scalars(
                select(DocumentVersion)
                .where(
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
        expected_total = 0
        for version in versions:
            chunks = list(
                db.scalars(
                    select(Chunk)
                    .where(Chunk.version_id == version.id)
                    .order_by(Chunk.ordinal)
                )
            )
            if not chunks:
                continue
            expected_total += len(chunks)
            if not args.bm25_only:
                missing = [
                    chunk
                    for chunk in chunks
                    if not db.scalar(
                        select(ChunkEmbedding).where(
                            ChunkEmbedding.chunk_id == chunk.id,
                            ChunkEmbedding.embedding_fingerprint
                            == settings.embedding_fingerprint,
                        )
                    )
                ]
                if missing:
                    text_chunks = [
                        TextChunk(
                            ordinal=chunk.ordinal,
                            content=chunk.content,
                            content_hash=chunk.content_hash,
                            token_count=chunk.token_count,
                            heading_path=chunk.heading_path,
                            page_number=chunk.page_number,
                            sheet_name=chunk.sheet_name,
                            cell_range=chunk.cell_range,
                        )
                        for chunk in missing
                    ]
                    cache = ingestion._embeddings(db, text_chunks, [])
                    for chunk in missing:
                        cached = cache.get(chunk.content_hash)
                        if cached:
                            db.add(
                                ChunkEmbedding(
                                    chunk_id=chunk.id,
                                    embedding_fingerprint=settings.embedding_fingerprint,
                                    cache_id=cached.id,
                                )
                            )
                    db.commit()
            _, documents = indexer._documents_for_version(db, version.id)
            if args.bm25_only:
                for document in documents:
                    document.pop("embedding", None)
            index.index_chunks(documents)
            actual = index.count_version(version.id)
            if actual != len(chunks):
                raise RuntimeError(
                    f"Version verification failed: {version.id}, "
                    f"expected={len(chunks)}, actual={actual}"
                )

        pruned = index.prune_ineligible()
        count = index.count_all()
        if count != expected_total:
            raise RuntimeError(
                f"PostgreSQL search verification failed: "
                f"expected={expected_total}, actual={count}"
            )
        reconciliation = indexer.reconcile(db, repair=False)
        if reconciliation["difference_count"]:
            raise RuntimeError("PostgreSQL search manifest reconciliation failed")

    print(
        json.dumps(
            {
                "status": "succeeded",
                "backend": settings.search_backend,
                "model": settings.qwen_embedding_model,
                "embedding_fingerprint": settings.embedding_fingerprint,
                "dimensions": settings.qwen_embedding_dimensions,
                "chunks": expected_total,
                "pruned_rows": pruned,
                "index": index.trace_index_name(),
                "bm25_only": args.bm25_only,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
