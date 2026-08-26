"""Durable document parsing, chunking, embedding, and outbox preparation.

The ingestion worker writes PostgreSQL and immutable object artifacts first.
Search projection publication is delegated to the outbox-driven indexer so a
provider or cluster outage cannot lose accepted document uploads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentArtifact,
    DocumentVersion,
    EmbeddingCache,
    EmbeddingModel,
    IngestionJob,
    OutboxEvent,
)
from app.integrations.qwen import QwenClient
from app.integrations.search import SearchIndex
from app.integrations.storage import LocalObjectStorage
from app.knowledge.chunking import TextChunk, chunk_units
from app.knowledge.parsers import DocumentParser
from app.services.indexing import IndexingService

logger = logging.getLogger(__name__)
CHUNK_NAMESPACE = uuid.UUID("73ac24df-296f-4532-9dc8-e5890e877564")


def _record_hash(chunk: TextChunk) -> str:
    """计算搜索记录的稳定哈希，用于后续校验索引内容是否发生漂移。"""

    payload = json.dumps(
        {
            "content": chunk.content,
            "heading_path": chunk.heading_path,
            "page_number": chunk.page_number,
            "sheet_name": chunk.sheet_name,
            "cell_range": chunk.cell_range,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IngestionService:
    """Execute restartable ingestion jobs claimed with database leases."""

    def __init__(
        self,
        settings: Settings,
        parser: DocumentParser,
        qwen: QwenClient,
        index: SearchIndex,
        indexer: IndexingService | None = None,
        storage: LocalObjectStorage | None = None,
    ):
        """组装解析、向量化、对象存储和索引发布所需的依赖。"""

        self.settings = settings
        self.parser = parser
        self.qwen = qwen
        self.index = index
        self.storage = storage or LocalObjectStorage(settings.storage_root)
        self.indexer = indexer or IndexingService(settings, index, self.storage)

    def claim_next_job(self, db: Session) -> str | None:
        """以数据库租约领取一个任务，允许多个 Worker 安全并发消费。"""

        now = datetime.now(UTC)
        statement = (
            select(IngestionJob)
            .where(
                or_(
                    IngestionJob.status == "queued",
                    (
                        (IngestionJob.status == "running")
                        & (IngestionJob.lease_until.is_not(None))
                        & (IngestionJob.lease_until < now)
                    ),
                )
            )
            .order_by(IngestionJob.created_at, IngestionJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        job = db.scalar(statement)
        if not job:
            db.rollback()
            return None
        job.status = "running"
        job.stage = "starting"
        job.progress = 1
        job.attempts += 1
        job.started_at = now
        job.lease_until = now + timedelta(minutes=15)
        db.commit()
        return job.id

    def _ensure_embedding_model(self, db: Session) -> None:
        """登记当前向量空间；相同指纹代表向量可以安全复用。"""

        fingerprint = self.settings.embedding_fingerprint
        if not db.get(EmbeddingModel, fingerprint):
            try:
                with db.begin_nested():
                    db.add(
                        EmbeddingModel(
                            fingerprint=fingerprint,
                            model_id=self.settings.qwen_embedding_model,
                            dimensions=self.settings.qwen_embedding_dimensions,
                            preprocess_version=self.settings.embedding_preprocess_version,
                        )
                    )
                    db.flush()
            except IntegrityError:
                # 并发 Worker 可能已登记同一个不可变向量空间，此时直接复用即可。
                pass
            db.commit()

    @staticmethod
    def _missing_embedding_chunks(
        chunks: list[TextChunk],
        cached_hashes: set[str],
    ) -> list[TextChunk]:
        """按内容哈希去重，返回本次真正需要请求向量的分块。"""

        missing: list[TextChunk] = []
        seen = set(cached_hashes)
        for chunk in chunks:
            if chunk.content_hash not in seen:
                missing.append(chunk)
                seen.add(chunk.content_hash)
        return missing

    def _load_embedding_cache(
        self,
        db: Session,
        chunks: list[TextChunk],
        fingerprint: str,
    ) -> dict[str, EmbeddingCache]:
        """批量读取当前向量空间中已存在的内容缓存。"""

        hashes = list(dict.fromkeys(chunk.content_hash for chunk in chunks))
        return {
            row.content_hash: row
            for row in db.scalars(
                select(EmbeddingCache).where(
                    EmbeddingCache.content_hash.in_(hashes),
                    EmbeddingCache.embedding_fingerprint == fingerprint,
                )
            )
        }

    def _save_embedding_cache(
        self,
        db: Session,
        *,
        chunk: TextChunk,
        vector: list[float],
        fingerprint: str,
    ) -> EmbeddingCache:
        """保存一个内容寻址向量，并安全复用并发 Worker 的胜出记录。"""

        uri, checksum, _ = self.storage.save_vector(fingerprint, chunk.content_hash, vector)
        new_cache = EmbeddingCache(
            content_hash=chunk.content_hash,
            embedding_fingerprint=fingerprint,
            object_uri=uri,
            checksum=checksum,
            dimensions=len(vector),
        )
        try:
            with db.begin_nested():
                db.add(new_cache)
                db.flush()
            return new_cache
        except IntegrityError:
            # 唯一键冲突说明其他 Worker 已写入相同内容；按内容寻址可安全复用。
            cache = db.scalar(
                select(EmbeddingCache).where(
                    EmbeddingCache.content_hash == chunk.content_hash,
                    EmbeddingCache.embedding_fingerprint == fingerprint,
                )
            )
            if cache is None:
                raise
            return cache

    def _embed_batch(
        self,
        db: Session,
        batch: list[TextChunk],
        fingerprint: str,
        cached: dict[str, EmbeddingCache],
    ) -> None:
        """请求并校验一批向量，然后提交对应缓存元数据。"""

        vectors, _ = self.qwen.embeddings([chunk.content for chunk in batch])
        if len(vectors) != len(batch):
            raise ValueError("Embedding count does not match chunk count")
        for chunk, vector in zip(batch, vectors):
            if len(vector) != self.settings.qwen_embedding_dimensions:
                raise ValueError(
                    "Embedding dimension mismatch: expected "
                    f"{self.settings.qwen_embedding_dimensions}, got {len(vector)}"
                )
            cached[chunk.content_hash] = self._save_embedding_cache(
                db,
                chunk=chunk,
                vector=vector,
                fingerprint=fingerprint,
            )
        db.commit()

    def _generate_missing_embeddings(
        self,
        db: Session,
        missing: list[TextChunk],
        fingerprint: str,
        cached: dict[str, EmbeddingCache],
    ) -> None:
        """以十条为一批生成缺失向量，避免触发供应商载荷上限。"""

        for start in range(0, len(missing), 10):
            self._embed_batch(db, missing[start : start + 10], fingerprint, cached)

    def _embeddings(
        self,
        db: Session,
        chunks: list[TextChunk],
        warnings: list[str],
    ) -> dict[str, EmbeddingCache]:
        """优先复用内容寻址的向量缓存，仅为缺失内容批量请求新向量。"""

        self._ensure_embedding_model(db)
        fingerprint = self.settings.embedding_fingerprint
        cached = self._load_embedding_cache(db, chunks, fingerprint)
        missing_chunks = self._missing_embedding_chunks(chunks, set(cached))
        try:
            self._generate_missing_embeddings(db, missing_chunks, fingerprint, cached)
        except Exception as exc:
            db.rollback()
            if not self.settings.allow_bm25_only:
                raise
            # 开启降级时保留词法索引能力，避免向量服务故障阻塞整个入库链路。
            warnings.append(f"Embedding unavailable; indexed for BM25 only ({type(exc).__name__})")
        return cached

    def _save_normalized_artifact(
        self,
        db: Session,
        version: DocumentVersion,
        units: list[object],
        warnings: list[str],
    ) -> None:
        """保存规范化解析产物，供审计、排障和不重新解析的重建流程使用。"""

        payload = {
            "schema_version": 1,
            "parser_fingerprint": version.parser_fingerprint,
            "warnings": warnings,
            "units": [asdict(unit) for unit in units],
        }
        uri, sha256, byte_size = self.storage.save_json(
            (
                f"artifacts/{version.document_id}/{version.id}/"
                f"normalized-{version.parser_fingerprint}.json.zlib"
            ),
            payload,
        )
        artifact = db.scalar(
            select(DocumentArtifact).where(
                DocumentArtifact.version_id == version.id,
                DocumentArtifact.artifact_type == "normalized",
                DocumentArtifact.fingerprint == version.parser_fingerprint,
            )
        )
        if not artifact:
            db.add(
                DocumentArtifact(
                    version_id=version.id,
                    artifact_type="normalized",
                    object_uri=uri,
                    sha256=sha256,
                    byte_size=byte_size,
                    fingerprint=version.parser_fingerprint or "native-v1",
                )
            )
        else:
            artifact.object_uri = uri
            artifact.sha256 = sha256
            artifact.byte_size = byte_size

    def _persist_chunks_and_event(
        self,
        db: Session,
        job: IngestionJob,
        version: DocumentVersion,
        text_chunks: list[TextChunk],
        cache: dict[str, EmbeddingCache],
        warnings: list[str],
    ) -> str:
        """原子写入分块、向量关联和 Outbox 事件，并推进任务状态。"""

        old_ids = list(db.scalars(select(Chunk.id).where(Chunk.version_id == version.id)))
        if old_ids:
            db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(old_ids)))
            db.execute(delete(Chunk).where(Chunk.id.in_(old_ids)))
        chunk_ids = [
            str(
                uuid.uuid5(
                    CHUNK_NAMESPACE,
                    f"{version.id}:{item.ordinal}:{item.content_hash}",
                )
            )
            for item in text_chunks
        ]
        embedding_links: list[tuple[str, EmbeddingCache]] = []
        for position, (text_chunk, chunk_id) in enumerate(zip(text_chunks, chunk_ids)):
            chunk = Chunk(
                id=chunk_id,
                version_id=version.id,
                ordinal=text_chunk.ordinal,
                heading_path=text_chunk.heading_path,
                page_number=text_chunk.page_number,
                sheet_name=text_chunk.sheet_name,
                cell_range=text_chunk.cell_range,
                content=text_chunk.content,
                content_hash=text_chunk.content_hash,
                record_hash=_record_hash(text_chunk),
                token_count=text_chunk.token_count,
                previous_chunk_id=chunk_ids[position - 1] if position else None,
                next_chunk_id=chunk_ids[position + 1] if position + 1 < len(chunk_ids) else None,
            )
            db.add(chunk)
            embedding = cache.get(text_chunk.content_hash)
            if embedding:
                embedding_links.append((chunk_id, embedding))
        # ChunkEmbedding 只保存标量外键、没有 ORM relationship，SQLAlchemy 无法推断
        # 插入顺序；先 flush 父 Chunk，才能安全写入向量关联。
        db.flush()
        for chunk_id, embedding in embedding_links:
            db.add(
                ChunkEmbedding(
                    chunk_id=chunk_id,
                    embedding_fingerprint=self.settings.embedding_fingerprint,
                    cache_id=embedding.id,
                )
            )
        event = OutboxEvent(
            aggregate_id=version.id,
            event_type=(
                "delete_version" if version.lifecycle_status == "deprecated" else "index_version"
            ),
            payload={"job_id": job.id},
        )
        db.add(event)
        version.technical_status = "index_pending"
        version.parse_warnings = warnings
        job.status = "index_pending"
        job.stage = "indexing"
        job.progress = 75
        job.warnings = warnings
        job.lease_until = None
        db.commit()
        return event.id

    def process(self, db: Session, job_id: str) -> str:
        """执行可重试的解析→切块→向量化流程，并生成待发布索引事件。"""

        job = db.get(IngestionJob, job_id)
        if not job:
            raise ValueError(f"Unknown ingestion job: {job_id}")
        version = db.scalar(
            select(DocumentVersion)
            .options(joinedload(DocumentVersion.document).joinedload(Document.project))
            .where(DocumentVersion.id == job.version_id)
        )
        if not version:
            raise ValueError(f"Unknown document version: {job.version_id}")
        try:
            # 每个阶段先持久化状态，进程异常退出后运维端仍能定位失败位置。
            job.status = "running"
            job.stage, job.progress = "parsing", 10
            job.lease_until = datetime.now(UTC) + timedelta(minutes=15)
            version.technical_status = "parsing"
            version.parser_fingerprint = f"{self.settings.parser_backend}-{DocumentParser.revision}"
            version.chunker_fingerprint = (
                f"structure-v1-t{self.settings.chunk_target_tokens}"
                f"-m{self.settings.chunk_max_tokens}-o{self.settings.chunk_overlap_tokens}"
            )
            db.commit()
            source_path = self.storage.resolve(version.storage_path)
            units, warnings = self.parser.parse(source_path)
            text_chunks = chunk_units(
                units,
                target_tokens=self.settings.chunk_target_tokens,
                max_tokens=self.settings.chunk_max_tokens,
                overlap_tokens=self.settings.chunk_overlap_tokens,
            )
            if not text_chunks:
                raise ValueError("Parser produced no chunks")
            self._save_normalized_artifact(db, version, units, warnings)
            version.technical_status = "chunked"
            job.stage, job.progress = "embedding", 35
            job.warnings = warnings
            db.commit()
            cache = self._embeddings(db, text_chunks, warnings)
            version.technical_status = (
                "embedded"
                if len(cache) == len({item.content_hash for item in text_chunks})
                else "bm25_only"
            )
            event_id = self._persist_chunks_and_event(
                db, job, version, text_chunks, cache, warnings
            )
            return event_id
        except Exception as exc:
            db.rollback()
            job = db.get(IngestionJob, job_id)
            if job and job.status not in {"index_pending", "succeeded"}:
                job.status = "failed"
                job.stage = "failed"
                job.error_message = f"{type(exc).__name__}: {str(exc)[:1000]}"
                job.lease_until = None
                job.finished_at = datetime.now(UTC)
                version = db.get(DocumentVersion, job.version_id)
                if version:
                    version.technical_status = "failed_final"
                db.commit()
            logger.exception("Ingestion job %s failed", job_id)
            raise
