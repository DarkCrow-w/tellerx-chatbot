"""Offline-safe full-index rebuild and alias-switch command."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.container import qwen_client, search_index
from app.db import SessionLocal
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    DocumentVersion,
    IndexGeneration,
    IndexSyncState,
)
from app.integrations.storage import LocalObjectStorage
from app.knowledge.chunking import TextChunk
from app.knowledge.parsers import DocumentParser
from app.services.indexing import IndexingService, version_manifest_hash
from app.services.ingestion import IngestionService


def _target_name(base: str, generation: str | None, exists: bool) -> str:
    if generation:
        safe = "".join(character for character in generation if character.isalnum() or character in "-_")
        if not safe:
            raise ValueError("Generation must contain letters, digits, dashes, or underscores")
        return f"{base.rsplit('-', 1)[0]}-{safe}"
    if not exists:
        return base
    return f"{base.rsplit('-', 1)[0]}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and verify a new Elasticsearch generation")
    parser.add_argument("--generation")
    parser.add_argument("--bm25-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    index = search_index()
    target = _target_name(
        settings.search_index_name,
        args.generation,
        bool(index.client.indices.exists(index=settings.search_index_name)),
    )
    if index.client.indices.exists(index=target):
        raise SystemExit(
            f"Refusing to reuse existing Elasticsearch generation: {target}. "
            "Choose a new --generation value."
        )
    started = time.perf_counter()
    storage = LocalObjectStorage(settings.storage_root)
    ingestion = IngestionService(
        settings,
        DocumentParser(settings.parser_backend),
        qwen_client(),
        index,
        IndexingService(settings, index, storage),
        storage,
    )

    with SessionLocal() as db:
        generation = IndexGeneration(
            physical_index=target,
            schema_version=str(settings.elasticsearch_schema_version),
            embedding_fingerprint=settings.embedding_fingerprint,
            status="building",
        )
        db.add(generation)
        db.commit()
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
                    DocumentVersion.technical_status.not_in(["failed_final", "deleted"]),
                )
                .order_by(DocumentVersion.id)
            )
        )
        expected_total = 0
        try:
            index.create_index(target)
            for version in versions:
                chunks = list(
                    db.scalars(
                        select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal)
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
                                ChunkEmbedding.embedding_fingerprint == settings.embedding_fingerprint,
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
                _, documents = ingestion.indexer._documents_for_version(db, version.id)
                if args.bm25_only:
                    for document in documents:
                        document.pop("embedding", None)
                index.index_chunks(documents, target_index=target)
                actual = index.count_version(version.id, target_index=target)
                if actual != len(chunks):
                    raise RuntimeError(
                        f"Version verification failed: {version.id}, expected={len(chunks)}, actual={actual}"
                    )
                manifest = version_manifest_hash(chunks)
                db.add(
                    IndexSyncState(
                        version_id=version.id,
                        generation_id=generation.id,
                        expected_chunks=len(chunks),
                        indexed_chunks=actual,
                        manifest_hash=manifest,
                        status="verified",
                        verified_at=datetime.now(UTC),
                    )
                )
                db.commit()
            count = int(index.client.count(index=target).get("count", 0))
            if count != expected_total:
                raise RuntimeError(
                    f"Generation verification failed: expected={expected_total}, actual={count}"
                )
            manifests = list(
                db.scalars(
                    select(IndexSyncState.manifest_hash)
                    .where(IndexSyncState.generation_id == generation.id)
                    .order_by(IndexSyncState.version_id)
                )
            )
            generation.expected_chunks = expected_total
            generation.indexed_chunks = count
            generation.manifest_hash = hashlib.sha256("\n".join(manifests).encode()).hexdigest()
            index.activate_alias(target)
            generation.status = "active"
            generation.activated_at = datetime.now(UTC)
            for old in db.scalars(
                select(IndexGeneration).where(
                    IndexGeneration.id != generation.id,
                    IndexGeneration.status == "active",
                )
            ):
                old.status = "retired"
            db.commit()
        except Exception:
            db.rollback()
            generation = db.get(IndexGeneration, generation.id)
            if generation:
                generation.status = "failed"
                db.commit()
            raise

    print(
        json.dumps(
            {
                "status": "succeeded",
                "model": settings.qwen_embedding_model,
                "embedding_fingerprint": settings.embedding_fingerprint,
                "dimensions": settings.qwen_embedding_dimensions,
                "chunks": expected_total,
                "index": target,
                "bm25_only": args.bm25_only,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
