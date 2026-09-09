"""入库的章节、分块和向量关联写入；事务由调用方提交。"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Chunk, ChunkEmbedding, DocumentSection, DocumentVersion, EmbeddingCache
from app.knowledge.chunking import TextChunk
from app.knowledge.document_scope import normalize_document_name

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
            "section_key": chunk.section_key,
            "section_path": chunk.section_path,
            "embedding_input_hash": chunk.embedding_input_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def remove_version_chunks(db: Session, version_id: str) -> None:
    old_ids = list(db.scalars(select(Chunk.id).where(Chunk.version_id == version_id)))
    if old_ids:
        db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(old_ids)))
        db.execute(delete(Chunk).where(Chunk.id.in_(old_ids)))
    db.execute(delete(DocumentSection).where(DocumentSection.version_id == version_id))


def write_sections(
    db: Session, version: DocumentVersion, text_chunks: list[TextChunk]
) -> dict[str, str]:
    root_id = str(uuid.uuid5(CHUNK_NAMESPACE, f"{version.id}:section:root"))
    root = DocumentSection(
        id=root_id,
        version_id=version.id,
        section_key="root",
        level=0,
        title=version.document.filename,
        normalized_title=normalize_document_name(version.document.filename),
        heading_path="",
        ordinal=0,
        page_start=min(
            (item.page_number for item in text_chunks if item.page_number is not None),
            default=None,
        ),
        page_end=max(
            (item.page_number for item in text_chunks if item.page_number is not None),
            default=None,
        ),
    )
    db.add(root)
    db.flush()

    section_chunks: dict[str, list[TextChunk]] = {}
    section_order: list[str] = []
    for item in text_chunks:
        key = item.section_key or "root"
        if key not in section_chunks:
            section_order.append(key)
            section_chunks[key] = []
        section_chunks[key].append(item)
    section_ids = {"root": root_id}
    for key in section_order:
        if key == "root":
            continue
        section_ids[key] = str(uuid.uuid5(CHUNK_NAMESPACE, f"{version.id}:section:{key}"))
    for ordinal, key in enumerate(section_order, start=1):
        if key == "root":
            continue
        items = section_chunks[key]
        sample = items[0]
        page_numbers = [item.page_number for item in items if item.page_number is not None]
        db.add(
            DocumentSection(
                id=section_ids[key],
                version_id=version.id,
                parent_section_id=section_ids.get(sample.parent_section_key or "root", root_id),
                section_key=key,
                level=max(1, sample.section_level),
                title=sample.section_title or sample.heading_path or version.document.filename,
                normalized_title=normalize_document_name(
                    sample.section_title or sample.heading_path or version.document.filename
                ),
                heading_path=sample.heading_path or "",
                ordinal=ordinal,
                page_start=min(page_numbers, default=None),
                page_end=max(page_numbers, default=None),
            )
        )
        db.flush()
    return section_ids


def write_chunks(
    db: Session,
    version: DocumentVersion,
    text_chunks: list[TextChunk],
    section_ids: dict[str, str],
    cache: dict[str, EmbeddingCache],
    embedding_fingerprint: str,
) -> None:
    root_id = section_ids["root"]
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
    lead_chunk_by_section: dict[str, str] = {}
    for text_chunk, chunk_id in zip(text_chunks, chunk_ids):
        lead_chunk_by_section.setdefault(text_chunk.section_key or "root", chunk_id)
    for position, (text_chunk, chunk_id) in enumerate(zip(text_chunks, chunk_ids)):
        section_key = text_chunk.section_key or "root"
        section_lead = lead_chunk_by_section[section_key]
        parent_chunk_id = None
        if section_lead != chunk_id:
            parent_chunk_id = section_lead
        elif text_chunk.parent_section_key:
            parent_chunk_id = lead_chunk_by_section.get(text_chunk.parent_section_key)
        chunk = Chunk(
            id=chunk_id,
            version_id=version.id,
            section_id=section_ids.get(section_key, root_id),
            ordinal=text_chunk.ordinal,
            heading_path=text_chunk.heading_path,
            page_number=text_chunk.page_number,
            sheet_name=text_chunk.sheet_name,
            cell_range=text_chunk.cell_range,
            content=text_chunk.content,
            content_hash=text_chunk.content_hash,
            embedding_input_hash=text_chunk.embedding_input_hash or text_chunk.content_hash,
            record_hash=_record_hash(text_chunk),
            token_count=text_chunk.token_count,
            parent_chunk_id=parent_chunk_id,
            previous_chunk_id=chunk_ids[position - 1] if position else None,
            next_chunk_id=chunk_ids[position + 1] if position + 1 < len(chunk_ids) else None,
        )
        db.add(chunk)
        embedding = cache.get(text_chunk.embedding_input_hash or text_chunk.content_hash)
        if embedding:
            embedding_links.append((chunk_id, embedding))
    # ChunkEmbedding 只保存标量外键、没有 ORM relationship，SQLAlchemy 无法推断
    # 插入顺序；先 flush 父 Chunk，才能安全写入向量关联。
    db.flush()
    for chunk_id, embedding in embedding_links:
        db.add(
            ChunkEmbedding(
                chunk_id=chunk_id,
                embedding_fingerprint=embedding_fingerprint,
                cache_id=embedding.id,
            )
        )
