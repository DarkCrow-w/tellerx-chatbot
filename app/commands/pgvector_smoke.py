from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import create_engine, delete
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Chunk, Document, DocumentVersion, Project
from app.integrations.search import SearchIndex


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector(axis: int) -> list[float]:
    values = [0.0] * 1024
    values[axis] = 1.0
    return values


def _top_chunk(hits: list[dict[str, Any]]) -> str | None:
    return str(hits[0]["_id"]) if hits else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run destructive-isolated PostgreSQL FTS and pgvector smoke checks"
    )
    parser.add_argument(
        "--allow-shared-database",
        action="store_true",
        help="Allow running outside a database whose name ends in _verify",
    )
    args = parser.parse_args()
    settings = Settings()
    url = make_url(settings.database_url)
    database_name = url.database or ""
    if not settings.database_url.startswith("postgresql"):
        parser.error("DATABASE_URL must use PostgreSQL")
    if not args.allow_shared_database and not database_name.endswith("_verify"):
        parser.error(
            "Refusing to insert smoke fixtures into a shared database; "
            "use a dedicated database ending in _verify"
        )

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    index = SearchIndex(settings, engine=engine)
    index.ensure_index()
    baseline_count = index.count_all()
    run_id = uuid.uuid4().hex[:10]
    project_ids: list[str] = []
    document_ids: list[str] = []
    version_ids: list[str] = []
    chunk_ids: list[str] = []
    documents_for_index: list[dict[str, Any]] = []

    fixtures = (
        (
            "primary",
            "翠湖授信",
            "CTL-4616",
            "失败请求必须进入 MANUAL-QUEUE-4616 人工队列。",
            0,
        ),
        (
            "distractor",
            "蓝河结算",
            "CTL-9999",
            "失败请求等待十五分钟后重试。",
            1,
        ),
    )
    try:
        with Session(engine) as db:
            for label, business, control_id, rule, axis in fixtures:
                project = Project(name=f"pgvector-smoke-{run_id}-{label}")
                db.add(project)
                db.flush()
                document = Document(
                    project_id=project.id,
                    logical_key=f"smoke/{label}",
                    filename=f"{label}-{control_id}.md",
                    document_type="verification",
                    visibility="public",
                )
                db.add(document)
                db.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    sha256=_digest(f"{run_id}:{label}"),
                    storage_path=f"smoke/{run_id}/{label}.md",
                    lifecycle_status="approved",
                    technical_status="searchable",
                    is_current=True,
                    version_label="verify-1",
                )
                db.add(version)
                db.flush()
                content = f"业务名称：{business}\n控制编号：{control_id}\n处理规则：{rule}"
                chunk = Chunk(
                    version_id=version.id,
                    ordinal=0,
                    heading_path="当前批准控制规则",
                    content=content,
                    content_hash=_digest(content),
                    record_hash=_digest(f"record:{content}"),
                    token_count=40,
                )
                db.add(chunk)
                db.flush()
                project_ids.append(project.id)
                document_ids.append(document.id)
                version_ids.append(version.id)
                chunk_ids.append(chunk.id)
                documents_for_index.append(
                    {
                        "chunk_id": chunk.id,
                        "filename": document.filename,
                        "title_path": chunk.heading_path,
                        "content": chunk.content,
                        "record_hash": chunk.record_hash,
                        "embedding": _vector(axis),
                    }
                )
            db.commit()

        index.index_chunks(documents_for_index)
        lexical = index.lexical_search(
            "CTL-4616 的失败处理路径是什么？",
            [project_ids[0]],
            ["approved"],
            5,
        )
        chinese = index.lexical_search(
            "翠湖授信失败后进入哪个队列？",
            [project_ids[0]],
            ["approved"],
            5,
        )
        vector = index.vector_search(
            _vector(0),
            [project_ids[0]],
            ["approved"],
            5,
        )
        wrong_project = index.lexical_search(
            "CTL-4616 的失败处理路径是什么？",
            [project_ids[1]],
            ["approved"],
            5,
        )
        checks = {
            "exact_identifier_fts": _top_chunk(lexical) == chunk_ids[0],
            "chinese_fts": _top_chunk(chinese) == chunk_ids[0],
            "pgvector_cosine": _top_chunk(vector) == chunk_ids[0],
            "project_filter": (
                all(
                    str(hit.get("_source", {}).get("project_id")) == project_ids[1]
                    for hit in wrong_project
                )
                and chunk_ids[0] not in {
                    str(hit.get("_id")) for hit in wrong_project
                }
            ),
            "version_count": index.count_version(version_ids[0]) == 1,
            "status_ready": bool(index.status().get("available")),
        }
        if not all(checks.values()):
            raise RuntimeError(f"pgvector smoke verification failed: {checks}")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "database": database_name,
                    "backend": settings.search_backend,
                    "checks": checks,
                    "baseline_rows": baseline_count,
                },
                ensure_ascii=False,
            )
        )
    finally:
        for version_id in version_ids:
            index.delete_version(version_id)
        with Session(engine) as db:
            if chunk_ids:
                db.execute(delete(Chunk).where(Chunk.id.in_(chunk_ids)))
            if version_ids:
                db.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(version_ids)))
            if document_ids:
                db.execute(delete(Document).where(Document.id.in_(document_ids)))
            if project_ids:
                db.execute(delete(Project).where(Project.id.in_(project_ids)))
            db.commit()
        remaining = index.count_all()
        if remaining != baseline_count:
            raise RuntimeError(
                f"Smoke fixture cleanup failed: baseline={baseline_count}, remaining={remaining}"
            )


if __name__ == "__main__":
    main()
