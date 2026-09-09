"""Durable document parsing, chunking, embedding, and outbox preparation.

The ingestion worker writes PostgreSQL and immutable object artifacts first.
Search projection publication is delegated to the outbox-driven indexer so a
provider or cluster outage cannot lose accepted document uploads.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings
from app.db.models import (
    Document,
    DocumentArtifact,
    DocumentVersion,
    EmbeddingCache,
    EmbeddingModel,
    IngestionJob,
    OutboxEvent,
)
from app.integrations.openai_client import ModelAPIError, OpenAIModelClient
from app.integrations.search import SearchIndex
from app.integrations.storage import LocalObjectStorage, storage_path_errors
from app.knowledge.chunking import TextChunk, chunk_units
from app.knowledge.parsers import DocumentParser
from app.repositories import ingestion_records
from app.services.indexing import IndexingService

logger = logging.getLogger(__name__)


class IngestionCancelled(Exception):
    """文档已删除时用于尽快终止解析或向量化的内部控制流。"""


class IngestionService:
    """Execute restartable ingestion jobs claimed with database leases."""

    def __init__(
        self,
        settings: Settings,
        parser: DocumentParser,
        model_client: OpenAIModelClient,
        index: SearchIndex,
        indexer: IndexingService | None = None,
        storage: LocalObjectStorage | None = None,
    ):
        """组装解析、向量化、对象存储和索引发布所需的依赖。"""

        self.settings = settings
        self.parser = parser
        self.model_client = model_client
        self.index = index
        self.storage = storage or LocalObjectStorage(settings.storage_root)
        self.indexer = indexer or IndexingService(settings, index, self.storage)

    @staticmethod
    def _prepare_embedding_inputs(chunks: list[TextChunk], document: Document) -> None:
        """为每个分块构造含文档和标题上下文、但仍保留原文引用的向量输入。"""

        for chunk in chunks:
            heading_path = chunk.heading_path or "（文档正文）"
            chunk.embedding_input = (
                f"文件名: {document.filename}\n"
                f"文档类型: {document.document_type}\n"
                f"标题路径: {heading_path}\n"
                f"正文:\n{chunk.content}"
            )
            chunk.embedding_input_hash = hashlib.sha256(
                chunk.embedding_input.encode("utf-8")
            ).hexdigest()

    def claim_next_job(self, db: Session) -> str | None:
        """以数据库租约领取一个任务，允许多个 Worker 安全并发消费。"""

        now = datetime.now(UTC)
        statement = (
            select(IngestionJob)
            .join(Document, IngestionJob.document_id == Document.id)
            .where(
                Document.is_deleted.is_(False),
                or_(
                    IngestionJob.status == "queued",
                    (
                        (IngestionJob.status == "running")
                        & (IngestionJob.lease_until.is_not(None))
                        & (IngestionJob.lease_until < now)
                    ),
                ),
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
        logger.info("已领取入库任务 job_id=%s attempt=%d", job.id, job.attempts)
        return job.id

    @staticmethod
    def _assert_document_active(db: Session, version_id: str) -> None:
        """在昂贵阶段之间重查删除状态，避免继续调用模型。"""

        is_deleted = db.scalar(
            select(Document.is_deleted)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.id == version_id)
        )
        if is_deleted is None or is_deleted:
            raise IngestionCancelled("Document was deleted during ingestion")

    def _ensure_embedding_model(self, db: Session) -> None:
        """登记当前向量空间；相同指纹代表向量可以安全复用。"""

        fingerprint = self.settings.embedding_fingerprint
        if not db.get(EmbeddingModel, fingerprint):
            try:
                with db.begin_nested():
                    db.add(
                        EmbeddingModel(
                            fingerprint=fingerprint,
                            model_id=self.settings.embedding_model,
                            dimensions=self.settings.embedding_dimensions,
                            preprocess_version=self.settings.embedding_preprocess_version,
                        )
                    )
                    db.flush()
            except IntegrityError:
                # 仅在并发请求确实已写入同一向量空间时复用，其他约束错误继续抛出。
                if db.get(EmbeddingModel, fingerprint) is None:
                    raise
            db.commit()

    @staticmethod
    def _missing_embedding_chunks(
        chunks: list[TextChunk],
        cached_hashes: set[str],
    ) -> list[TextChunk]:
        """按上下文化输入哈希去重，返回真正需要请求向量的分块。"""

        missing: list[TextChunk] = []
        seen = set(cached_hashes)
        for chunk in chunks:
            if chunk.embedding_input_hash not in seen:
                missing.append(chunk)
                seen.add(chunk.embedding_input_hash)
        return missing

    def _load_embedding_cache(
        self,
        db: Session,
        chunks: list[TextChunk],
        fingerprint: str,
    ) -> dict[str, EmbeddingCache]:
        """批量读取当前向量空间中已存在的内容缓存。"""

        hashes = list(dict.fromkeys(chunk.embedding_input_hash for chunk in chunks))
        return {
            row.embedding_input_hash: row
            for row in db.scalars(
                select(EmbeddingCache).where(
                    EmbeddingCache.embedding_input_hash.in_(hashes),
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

        if not chunk.embedding_input_hash:
            raise ValueError("Chunk is missing contextual embedding identity")
        uri, checksum, _ = self.storage.save_vector(fingerprint, chunk.embedding_input_hash, vector)
        new_cache = EmbeddingCache(
            content_hash=chunk.content_hash,
            embedding_input_hash=chunk.embedding_input_hash,
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
                    EmbeddingCache.embedding_input_hash == chunk.embedding_input_hash,
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

        vectors, _ = self.model_client.embeddings(
            [chunk.embedding_input or chunk.content for chunk in batch]
        )
        if len(vectors) != len(batch):
            raise ValueError("Embedding count does not match chunk count")
        for chunk, vector in zip(batch, vectors):
            if len(vector) != self.settings.embedding_dimensions:
                raise ValueError(
                    "Embedding dimension mismatch: expected "
                    f"{self.settings.embedding_dimensions}, got {len(vector)}"
                )
            if not chunk.embedding_input_hash:
                raise ValueError("Chunk is missing contextual embedding identity")
            cached[chunk.embedding_input_hash] = self._save_embedding_cache(
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
        *,
        version_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """以十条为一批生成缺失向量，避免触发供应商载荷上限。"""

        batch_count = (len(missing) + 9) // 10
        for start in range(0, len(missing), 10):
            if version_id is not None:
                self._assert_document_active(db, version_id)
            batch = missing[start : start + 10]
            batch_number = start // 10 + 1
            started = time.perf_counter()
            logger.info(
                "Embedding批次开始 job_id=%s batch=%d/%d inputs=%d",
                job_id or "-",
                batch_number,
                batch_count,
                len(batch),
            )
            self._embed_batch(db, batch, fingerprint, cached)
            logger.info(
                "Embedding批次完成 job_id=%s batch=%d/%d completed=%d/%d elapsed_ms=%.1f",
                job_id or "-",
                batch_number,
                batch_count,
                start + len(batch),
                len(missing),
                (time.perf_counter() - started) * 1000,
            )

    def _embeddings(
        self,
        db: Session,
        chunks: list[TextChunk],
        warnings: list[str],
        *,
        version_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, EmbeddingCache]:
        """优先复用内容寻址的向量缓存，仅为缺失内容批量请求新向量。"""

        started = time.perf_counter()
        self._ensure_embedding_model(db)
        fingerprint = self.settings.embedding_fingerprint
        cached = self._load_embedding_cache(db, chunks, fingerprint)
        missing_chunks = self._missing_embedding_chunks(chunks, set(cached))
        logger.info(
            "Embedding准备完成 job_id=%s chunks=%d unique_inputs=%d cached=%d missing=%d "
            "model=%s dimensions=%d",
            job_id or "-",
            len(chunks),
            len({chunk.embedding_input_hash for chunk in chunks}),
            len(cached),
            len(missing_chunks),
            self.settings.embedding_model,
            self.settings.embedding_dimensions,
        )
        try:
            self._generate_missing_embeddings(
                db,
                missing_chunks,
                fingerprint,
                cached,
                version_id=version_id,
                job_id=job_id,
            )
        except IngestionCancelled:
            raise
        except ModelAPIError as exc:
            db.rollback()
            if not self.settings.allow_bm25_only:
                raise
            # 开启降级时保留词法索引能力，避免向量服务故障阻塞整个入库链路。
            logger.warning(
                "Embedding服务失败，保留已缓存向量并使用关键词检索 "
                "job_id=%s cached=%d missing=%d status=%s code=%s elapsed_ms=%.1f",
                job_id or "-",
                len(cached),
                len(self._missing_embedding_chunks(chunks, set(cached))),
                exc.status_code,
                exc.code,
                (time.perf_counter() - started) * 1000,
            )
            warnings.append(f"Embedding unavailable; indexed for BM25 only ({type(exc).__name__})")
        else:
            logger.info(
                "Embedding阶段完成 job_id=%s cached=%d generated=%d elapsed_ms=%.1f",
                job_id or "-",
                len(cached),
                len(missing_chunks),
                (time.perf_counter() - started) * 1000,
            )
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
            "schema_version": 2,
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

        ingestion_records.remove_version_chunks(db, version.id)
        section_ids = ingestion_records.write_sections(db, version, text_chunks)
        ingestion_records.write_chunks(
            db, version, text_chunks, section_ids, cache, self.settings.embedding_fingerprint
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

    def process(self, db: Session, job_id: str) -> str | None:
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
        started = time.perf_counter()
        logger.info(
            "入库任务处理开始 job_id=%s filename=%s",
            job_id,
            version.document.filename,
        )
        logger.debug(
            "入库任务关联 job_id=%s document_id=%s version_id=%s",
            job_id,
            version.document_id,
            version.id,
        )
        stage = "parsing"
        try:
            self._assert_document_active(db, version.id)
            text_chunks, warnings = self._parse_document(db, job, version)
            stage = "embedding"
            cache = self._embeddings(
                db,
                text_chunks,
                warnings,
                version_id=version.id,
                job_id=job_id,
            )
            self._assert_document_active(db, version.id)
            version.technical_status = (
                "embedded"
                if len(cache) == len({item.embedding_input_hash for item in text_chunks})
                else "bm25_only"
            )
            stage = "persisting"
            event_id = self._persist_chunks_and_event(
                db, job, version, text_chunks, cache, warnings
            )
            logger.info(
                "入库数据写入完成，等待索引发布 job_id=%s event_id=%s "
                "chunks=%d embeddings=%d elapsed_ms=%.1f",
                job_id,
                event_id,
                len(text_chunks),
                len(cache),
                (time.perf_counter() - started) * 1000,
            )
            return event_id
        except IngestionCancelled:
            db.rollback()
            job = db.get(IngestionJob, job_id)
            if job is not None:
                job.status = "cancelled"
                job.stage = "cancelled"
                job.error_message = "Document was deleted before ingestion completed"
                job.lease_until = None
                job.finished_at = datetime.now(UTC)
                current_version = db.get(DocumentVersion, job.version_id)
                if current_version is not None:
                    current_version.technical_status = "deleted"
                    current_version.is_current = False
                db.commit()
            logger.info("入库任务因文档删除而取消 job_id=%s", job_id)
            return None
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
            logger.exception(
                "入库任务失败 job_id=%s stage=%s error=%s elapsed_ms=%.1f",
                job_id,
                stage,
                type(exc).__name__,
                (time.perf_counter() - started) * 1000,
            )
            raise

    def _parse_document(
        self,
        db: Session,
        job: IngestionJob,
        version: DocumentVersion,
    ) -> tuple[list[TextChunk], list[str]]:
        """持久化解析阶段，保存规范化原文，并准备有上下文的向量输入。"""
        # 每个阶段先持久化状态，进程异常退出后运维端仍能定位失败位置。
        job.status = "running"
        job.stage, job.progress = "parsing", 10
        job.lease_until = datetime.now(UTC) + timedelta(minutes=15)
        version.technical_status = "parsing"
        version.parser_fingerprint = f"{self.settings.parser_backend}-{DocumentParser.revision}"
        version.chunker_fingerprint = (
            f"structure-v2-t{self.settings.chunk_target_tokens}"
            f"-m{self.settings.chunk_max_tokens}-o{self.settings.chunk_overlap_tokens}"
        )
        db.commit()
        source_path = self.storage.resolve(version.storage_path)
        started = time.perf_counter()
        logger.info("文档解析开始 job_id=%s format=%s", job.id, source_path.suffix.lower())
        with storage_path_errors():
            units, warnings = self.parser.parse(source_path)
        logger.info(
            "文档解析完成 job_id=%s units=%d warnings=%d elapsed_ms=%.1f",
            job.id,
            len(units),
            len(warnings),
            (time.perf_counter() - started) * 1000,
        )
        started = time.perf_counter()
        logger.info(
            "文档切块开始 job_id=%s target_tokens=%d max_tokens=%d overlap_tokens=%d",
            job.id,
            self.settings.chunk_target_tokens,
            self.settings.chunk_max_tokens,
            self.settings.chunk_overlap_tokens,
        )
        text_chunks = chunk_units(
            units,
            target_tokens=self.settings.chunk_target_tokens,
            max_tokens=self.settings.chunk_max_tokens,
            overlap_tokens=self.settings.chunk_overlap_tokens,
        )
        if not text_chunks:
            raise ValueError("Parser produced no chunks")
        self._prepare_embedding_inputs(text_chunks, version.document)
        logger.info(
            "文档切块完成 job_id=%s chunks=%d total_tokens=%d "
            "min_chunk_tokens=%d max_chunk_tokens=%d elapsed_ms=%.1f",
            job.id,
            len(text_chunks),
            sum(chunk.token_count for chunk in text_chunks),
            min(chunk.token_count for chunk in text_chunks),
            max(chunk.token_count for chunk in text_chunks),
            (time.perf_counter() - started) * 1000,
        )
        self._assert_document_active(db, version.id)
        self._save_normalized_artifact(db, version, units, warnings)
        version.technical_status = "chunked"
        job.stage, job.progress = "embedding", 35
        job.warnings = warnings
        db.commit()
        return text_chunks, warnings
