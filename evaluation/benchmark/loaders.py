"""Corpus loading, indexing, and API smoke operations for benchmarks."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.core.container import qwen_client, search_index
from app.db import Base, SessionLocal, engine
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentArtifact,
    DocumentVersion,
    EmbeddingCache,
    EmbeddingModel,
    IndexGeneration,
    IndexSyncState,
    IngestionJob,
    OutboxEvent,
    Project,
)
from app.integrations.search import SearchIndex
from app.knowledge.chunking import chunk_units
from app.knowledge.parsers import DocumentParser
from app.services.ingestion import IngestionService
from evaluation.benchmark.corpus import _read_jsonl
from evaluation.benchmark.metrics import _latency_stats
from evaluation.benchmark.offline import OfflineBenchmarkQwen, _offline_feature_vector

logger = logging.getLogger(__name__)

KNOWLEDGE_RESET_ORDER = [
    IndexSyncState,
    IndexGeneration,
    OutboxEvent,
    ChunkEmbedding,
    EmbeddingCache,
    EmbeddingModel,
    DocumentArtifact,
    Chunk,
    IngestionJob,
    DocumentVersion,
    Document,
    Project,
]


def load_corpus(corpus_dir: Path, *, reset: bool, embedding: bool) -> dict[str, Any]:
    settings = Settings()
    parser = DocumentParser(settings.parser_backend)
    qwen = qwen_client()
    index = search_index()
    Base.metadata.create_all(engine)
    manifest = _read_jsonl(corpus_dir / "manifest.jsonl")
    started = time.perf_counter()
    parse_times: list[float] = []
    chunks_to_index: list[dict[str, Any]] = []
    format_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()

    with SessionLocal() as db:
        if reset:
            index.clear()
            for model in KNOWLEDGE_RESET_ORDER:
                db.execute(delete(model))
            db.commit()
        projects: dict[str, Project] = {}
        for position, row in enumerate(manifest, start=1):
            path = corpus_dir / row["path"]
            format_counts[path.suffix.lower()] += 1
            parse_start = time.perf_counter()
            units, warnings = parser.parse(path)
            parse_times.append((time.perf_counter() - parse_start) * 1000)
            for warning in warnings:
                warning_counts[warning.split(" (")[0]] += 1
            text_chunks = chunk_units(
                units,
                target_tokens=settings.chunk_target_tokens,
                max_tokens=settings.chunk_max_tokens,
                overlap_tokens=settings.chunk_overlap_tokens,
            )
            project = projects.get(row["project"])
            if not project:
                project = db.scalar(select(Project).where(Project.name == row["project"]))
                if not project:
                    project = Project(name=row["project"])
                    db.add(project)
                    db.flush()
                projects[row["project"]] = project
            document = db.scalar(
                select(Document).where(
                    Document.project_id == project.id,
                    Document.filename == row["logical_filename"],
                )
            )
            if not document:
                document = Document(
                    project_id=project.id,
                    logical_key=row["logical_filename"],
                    filename=row["logical_filename"],
                    document_type=row["document_type"],
                    source_type=row["source_type"],
                )
                db.add(document)
                db.flush()
            version = DocumentVersion(
                document_id=document.id,
                sha256=f"benchmark-{row['source_key']}",
                storage_path=row["path"],
                lifecycle_status=row["lifecycle_status"],
                technical_status="searchable",
                is_current=row["lifecycle_status"] == "approved",
                version_label=row["version_label"],
                indexed_at=datetime.now(UTC),
                searchable_at=datetime.now(UTC),
                parse_warnings=warnings,
            )
            db.add(version)
            db.flush()
            for text_chunk in text_chunks:
                chunk = Chunk(
                    version_id=version.id,
                    ordinal=text_chunk.ordinal,
                    heading_path=text_chunk.heading_path,
                    page_number=text_chunk.page_number,
                    sheet_name=text_chunk.sheet_name,
                    cell_range=text_chunk.cell_range,
                    content=text_chunk.content,
                    content_hash=text_chunk.content_hash,
                    record_hash=text_chunk.content_hash,
                    token_count=text_chunk.token_count,
                )
                db.add(chunk)
                db.flush()
                chunks_to_index.append(
                    {
                        "chunk_id": chunk.id,
                        "document_id": document.id,
                        "version_id": version.id,
                        "project_id": project.id,
                        "filename": document.filename,
                        "document_status": version.lifecycle_status,
                        "document_type": document.document_type,
                        "visibility": document.visibility,
                        "version_label": version.version_label,
                        "heading_path": chunk.heading_path,
                        "page_number": chunk.page_number,
                        "sheet_name": chunk.sheet_name,
                        "cell_range": chunk.cell_range,
                        "content": chunk.content,
                    }
                )
            if position % 100 == 0:
                db.commit()
                logger.info("Parsed %s/%s documents", position, len(manifest))
        db.commit()

    cache_hits = 0
    if embedding:
        cache_path = corpus_dir / "embedding-cache.jsonl"
        embedding_cache: dict[str, list[float]] = {}
        if cache_path.exists():
            for row in _read_jsonl(cache_path):
                if len(row.get("embedding", [])) == settings.qwen_embedding_dimensions:
                    embedding_cache[row["content_hash"]] = row["embedding"]
        missing: list[tuple[str, dict[str, Any]]] = []
        for item in chunks_to_index:
            content_hash = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            cached = embedding_cache.get(content_hash)
            if cached is not None:
                item["embedding"] = cached
                cache_hits += 1
            else:
                missing.append((content_hash, item))
        with cache_path.open("a", encoding="utf-8") as cache_file:
            for start in range(0, len(missing), 10):
                batch = missing[start : start + 10]
                vectors, _ = qwen.embeddings([item["content"] for _, item in batch])
                for (content_hash, item), vector in zip(batch, vectors):
                    item["embedding"] = vector
                    cache_file.write(
                        json.dumps(
                            {"content_hash": content_hash, "embedding": vector},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                cache_file.flush()
                if start and start % 500 == 0:
                    logger.info("Embedded %s/%s missing chunks", start, len(missing))
    index.index_chunks(chunks_to_index, target_index=settings.search_index_name)
    index.activate_alias(settings.search_index_name)
    elapsed = time.perf_counter() - started
    report = {
        "documents": len(manifest),
        "chunks": len(chunks_to_index),
        "embedding_enabled": embedding,
        "embedding_cache_hits": cache_hits,
        "elapsed_seconds": round(elapsed, 3),
        "documents_per_second": round(len(manifest) / elapsed, 2),
        "parse_latency_ms": _latency_stats(parse_times),
        "formats": dict(format_counts),
        "warnings": dict(warning_counts),
        "index": settings.search_index_name,
    }
    (corpus_dir / "load-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def load_via_production_pipeline_offline(corpus_dir: Path, *, reset: bool) -> dict[str, Any]:
    """Run every benchmark document through the real ingestion job service."""
    settings = Settings(
        storage_root=corpus_dir,
        parser_backend="native",
        allow_bm25_only=True,
        qwen_embedding_model="offline-production-pipeline",
    )
    index = SearchIndex(settings)
    service = IngestionService(
        settings,
        DocumentParser(settings.parser_backend),
        OfflineBenchmarkQwen(),  # type: ignore[arg-type]
        index,
    )
    Base.metadata.create_all(engine)
    manifest = _read_jsonl(corpus_dir / "manifest.jsonl")
    if reset:
        index.clear()
        with SessionLocal() as db:
            for model in KNOWLEDGE_RESET_ORDER:
                db.execute(delete(model))
            db.commit()
    index.ensure_index()
    index.activate_alias(settings.search_index_name)

    with SessionLocal() as db:
        projects: dict[str, Project] = {}
        for row in manifest:
            project = projects.get(row["project"])
            if not project:
                project = db.scalar(select(Project).where(Project.name == row["project"]))
                if not project:
                    project = Project(name=row["project"])
                    db.add(project)
                    db.flush()
                projects[row["project"]] = project
            document = db.scalar(
                select(Document).where(
                    Document.project_id == project.id,
                    Document.filename == row["logical_filename"],
                )
            )
            if not document:
                document = Document(
                    project_id=project.id,
                    logical_key=row["logical_filename"],
                    filename=row["logical_filename"],
                    document_type=row["document_type"],
                    source_type=row["source_type"],
                )
                db.add(document)
                db.flush()
            version = DocumentVersion(
                document_id=document.id,
                sha256=f"production-benchmark-{row['source_key']}",
                storage_path=row["path"],
                lifecycle_status=row["lifecycle_status"],
                version_label=row["version_label"],
            )
            db.add(version)
            db.flush()
            db.add(IngestionJob(document_id=document.id, version_id=version.id))
        db.commit()

    started = time.perf_counter()
    claim_latencies = []
    process_latencies = []
    processed = 0
    while True:
        with SessionLocal() as db:
            claim_started = time.perf_counter()
            job_id = service.claim_next_job(db)
            claim_latencies.append((time.perf_counter() - claim_started) * 1000)
        if not job_id:
            break
        with SessionLocal() as db:
            process_started = time.perf_counter()
            event_id = service.process(db, job_id)
            service.indexer.publish_event(db, event_id)
            process_latencies.append((time.perf_counter() - process_started) * 1000)
        processed += 1
        if processed % 100 == 0:
            logger.info("Production pipeline processed %s/%s jobs", processed, len(manifest))

    with SessionLocal() as db:
        status_counts = dict(
            db.execute(
                select(IngestionJob.status, func.count()).group_by(IngestionJob.status)
            ).all()
        )
        chunk_count = int(db.scalar(select(func.count()).select_from(Chunk)) or 0)
    report = {
        "mode": "production_ingestion_service_offline_embedding",
        "documents": len(manifest),
        "processed_jobs": processed,
        "job_statuses": status_counts,
        "chunks": chunk_count,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "claim_latency_ms": _latency_stats(claim_latencies),
        "process_latency_ms": _latency_stats(process_latencies),
    }
    report["acceptance"] = {
        "passed": processed == len(manifest)
        and status_counts == {"succeeded": len(manifest)}
        and chunk_count > 0,
        "checks": {
            "all_jobs_processed": processed == len(manifest),
            "all_jobs_succeeded": status_counts == {"succeeded": len(manifest)},
            "chunks_created": chunk_count > 0,
        },
    }
    (corpus_dir / "production-ingestion-offline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def evaluate_api_smoke_offline(corpus_dir: Path) -> dict[str, Any]:
    """Exercise public document/job/source/download APIs with every format."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    manifest = _read_jsonl(corpus_dir / "manifest.jsonl")
    samples = {}
    for row in manifest:
        samples.setdefault(Path(row["path"]).suffix.lower(), row)
    project_name = f"Benchmark-API-{uuid.uuid4().hex[:10]}"
    checks = {
        "uploads_accepted": True,
        "duplicate_idempotent": False,
        "jobs_succeeded": True,
        "sources_resolvable": True,
        "downloads_match": True,
    }
    uploaded: list[tuple[dict[str, Any], dict[str, Any]]] = []
    with tempfile.TemporaryDirectory(prefix="knowledge-api-smoke-") as temp_dir:
        settings = Settings(
            storage_root=Path(temp_dir),
            parser_backend="native",
            allow_bm25_only=True,
            qwen_embedding_model="offline-api-smoke",
        )
        index = SearchIndex(settings)
        index.ensure_index()
        index.activate_alias(settings.search_index_name)
        service = IngestionService(
            settings,
            DocumentParser(settings.parser_backend),
            OfflineBenchmarkQwen(),  # type: ignore[arg-type]
            index,
        )
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            with TestClient(app) as client:
                for suffix, row in sorted(samples.items()):
                    path = corpus_dir / row["path"]
                    with path.open("rb") as handle:
                        response = client.post(
                            "/api/v1/documents",
                            files={"file": (path.name, handle, "application/octet-stream")},
                            data={
                                "project": project_name,
                                "document_type": row["document_type"],
                                "lifecycle_status": "approved",
                                "version_label": "api-smoke-v1",
                            },
                        )
                    checks["uploads_accepted"] &= response.status_code == 202
                    uploaded.append((row, response.json()))

                first_row, first_upload = uploaded[0]
                first_path = corpus_dir / first_row["path"]
                with first_path.open("rb") as handle:
                    duplicate = client.post(
                        "/api/v1/documents",
                        files={"file": (first_path.name, handle, "application/octet-stream")},
                        data={
                            "project": project_name,
                            "document_type": first_row["document_type"],
                            "lifecycle_status": "approved",
                            "version_label": "api-smoke-v1",
                        },
                    )
                duplicate_body = duplicate.json()
                checks["duplicate_idempotent"] = (
                    duplicate.status_code == 202
                    and duplicate_body.get("duplicate") is True
                    and duplicate_body.get("version_id") == first_upload["version_id"]
                )

                for _, upload in uploaded:
                    with SessionLocal() as db:
                        event_id = service.process(db, upload["job_id"])
                        service.indexer.publish_event(db, event_id)
                    job_response = client.get(f"/api/v1/ingestion-jobs/{upload['job_id']}")
                    checks["jobs_succeeded"] &= (
                        job_response.status_code == 200
                        and job_response.json().get("status") == "succeeded"
                    )

                with SessionLocal() as db:
                    chunk_rows = db.execute(
                        select(Chunk, DocumentVersion, Document)
                        .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
                        .join(Document, DocumentVersion.document_id == Document.id)
                        .where(Document.id.in_([item[1]["document_id"] for item in uploaded]))
                    ).all()
                first_chunks = {}
                for chunk, version, document in chunk_rows:
                    first_chunks.setdefault(document.id, (chunk, version, document))
                for row, upload in uploaded:
                    chunk, version, _ = first_chunks[upload["document_id"]]
                    source = client.get(f"/api/v1/sources/{chunk.id}")
                    checks["sources_resolvable"] &= (
                        source.status_code == 200
                        and source.json().get("document_id") == upload["document_id"]
                    )
                    download = client.get(
                        f"/api/v1/documents/{upload['document_id']}/download",
                        params={"version_id": version.id},
                    )
                    original = (corpus_dir / row["path"]).read_bytes()
                    checks["downloads_match"] &= (
                        download.status_code == 200 and download.content == original
                    )
        finally:
            app.dependency_overrides.pop(get_settings, None)

        index.clear()

    document_ids = [item[1]["document_id"] for item in uploaded]
    version_ids = [item[1]["version_id"] for item in uploaded]
    with SessionLocal() as db:
        db.execute(delete(Chunk).where(Chunk.version_id.in_(version_ids)))
        db.execute(delete(IngestionJob).where(IngestionJob.document_id.in_(document_ids)))
        db.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(version_ids)))
        db.execute(delete(Document).where(Document.id.in_(document_ids)))
        db.execute(delete(Project).where(Project.name == project_name))
        db.commit()
    SearchIndex(Settings(qwen_embedding_model="offline-production-pipeline")).ensure_index()

    report = {
        "mode": "public_api_offline_smoke",
        "formats": sorted(samples),
        "uploaded_documents": len(uploaded),
        "checks": checks,
        "passed": all(checks.values()) and len(uploaded) == len(samples),
    }
    (corpus_dir / "api-smoke-offline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def index_existing(corpus_dir: Path, *, embedding: bool) -> dict[str, Any]:
    settings = Settings()
    qwen = qwen_client()
    index = search_index()
    started = time.perf_counter()
    with SessionLocal() as db:
        rows = db.execute(
            select(Chunk, DocumentVersion, Document, Project)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .join(Project, Document.project_id == Project.id)
            .order_by(Chunk.id)
        ).all()
    chunks_to_index = [
        {
            "chunk_id": chunk.id,
            "document_id": document.id,
            "version_id": version.id,
            "project_id": project.id,
            "filename": document.filename,
            "document_status": version.lifecycle_status,
            "document_type": document.document_type,
            "visibility": document.visibility,
            "version_label": version.version_label,
            "heading_path": chunk.heading_path,
            "page_number": chunk.page_number,
            "sheet_name": chunk.sheet_name,
            "cell_range": chunk.cell_range,
            "content": chunk.content,
        }
        for chunk, version, document, project in rows
    ]
    cache_hits = 0
    cache_path = corpus_dir / "embedding-cache.jsonl"
    embedding_cache = (
        {
            row["content_hash"]: row["embedding"]
            for row in _read_jsonl(cache_path)
            if len(row.get("embedding", [])) == settings.qwen_embedding_dimensions
        }
        if cache_path.exists()
        else {}
    )
    missing = []
    if embedding:
        for item in chunks_to_index:
            content_hash = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            if content_hash in embedding_cache:
                item["embedding"] = embedding_cache[content_hash]
                cache_hits += 1
            else:
                missing.append((content_hash, item))
        with cache_path.open("a", encoding="utf-8") as cache_file:
            for start in range(0, len(missing), 10):
                batch = missing[start : start + 10]
                vectors, _ = qwen.embeddings([item["content"] for _, item in batch])
                for (content_hash, item), vector in zip(batch, vectors):
                    item["embedding"] = vector
                    cache_file.write(
                        json.dumps(
                            {"content_hash": content_hash, "embedding": vector},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                cache_file.flush()
    index.clear()
    index.index_chunks(chunks_to_index, target_index=settings.search_index_name)
    index.activate_alias(settings.search_index_name)
    report = {
        "chunks": len(chunks_to_index),
        "embedding_enabled": embedding,
        "embedding_cache_hits": cache_hits,
        "embedding_cache_misses": len(missing),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "index": settings.search_index_name,
    }
    (corpus_dir / "index-existing-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def index_offline_hybrid(corpus_dir: Path) -> dict[str, Any]:
    """Build a local feature-hash vector index without Qwen or network access."""
    settings = Settings(qwen_embedding_model="offline-feature-hash-v1")
    index = SearchIndex(settings)
    started = time.perf_counter()
    with SessionLocal() as db:
        rows = db.execute(
            select(Chunk, DocumentVersion, Document, Project)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .join(Project, Document.project_id == Project.id)
            .order_by(Chunk.id)
        ).all()
    documents = []
    for chunk, version, document, project in rows:
        documents.append(
            {
                "chunk_id": chunk.id,
                "document_id": document.id,
                "version_id": version.id,
                "project_id": project.id,
                "filename": document.filename,
                "document_status": version.lifecycle_status,
                "document_type": document.document_type,
                "visibility": document.visibility,
                "version_label": version.version_label,
                "heading_path": chunk.heading_path,
                "page_number": chunk.page_number,
                "sheet_name": chunk.sheet_name,
                "cell_range": chunk.cell_range,
                "content": chunk.content,
                "embedding": _offline_feature_vector(chunk.content),
            }
        )
    index.clear()
    index.index_chunks(documents, target_index=settings.search_index_name)
    index.activate_alias(settings.search_index_name)
    report = {
        "mode": "offline_feature_hash",
        "disclaimer": "Validates PostgreSQL pgvector mechanics; not Qwen embedding quality.",
        "chunks": len(documents),
        "dimensions": settings.qwen_embedding_dimensions,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "index": settings.search_index_name,
    }
    (corpus_dir / "index-offline-hybrid-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
