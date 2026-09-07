"""Real PostgreSQL and Qwen stress evaluation, with resumable per-case records.

Run from repository root with .venv/bin/python -m evaluation.stress50.run.
Credentials are loaded in memory; only synthetic prompts/responses are recorded.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evaluation/generated/stress50"
LOCAL = threading.local()
FLASH_DATABASE = "tellerx_stress50_flash_20260907"


def configure(args):
    from dotenv import dotenv_values

    values = dotenv_values(args.model_env) if args.model_env else {}
    shared = args.connection_doc.read_text(encoding="utf-8")
    url = re.findall(r"DATABASE_URL\s*=\s*([^\s`]+)", shared)[0]
    from sqlalchemy.engine import make_url

    if args.embedding_model.endswith("-flash"):
        url = make_url(url).set(database=FLASH_DATABASE).render_as_string(hide_password=False)
    os.environ.update(
        {
            "DATABASE_URL": url,
            "MODEL_API_KEY": args.key_file.read_text(encoding="utf-8").strip(),
            "MODEL_API_BASE_URL": values.get("MODEL_API_BASE_URL")
            or values.get("QWEN_CHAT_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "EMBEDDING_MODEL": args.embedding_model,
            "EMBEDDING_DIMENSIONS": "1024" if args.embedding_model.endswith("-flash") else "2560",
            "EMBEDDING_PREPROCESS_VERSION": "hierarchical-context-v2",
            "MODEL_REGISTRY_PATH": str(ROOT / "config/models.yaml"),
            "STORAGE_ROOT": str(ROOT / "evaluation/pipeline-storage/stress50"),
            "ALLOW_BM25_ONLY": "false",
            "LOG_LEVEL": "WARNING",
            "RERANK_ENABLED": "false",
        }
    )
    chat_model = getattr(args, "chat_model", "qwen3.7-max-2026-06-08")
    os.environ["STRESS50_CHAT_MODEL"] = chat_model
    if chat_model != "qwen3.7-max-2026-06-08":
        registry_path = OUTPUT / ("models-" + re.sub(r"[^A-Za-z0-9_.-]", "_", chat_model) + ".json")
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps({"models": [{"id": chat_model, "tier": "all", "quota_tokens": 100000000, "priority": 10, "enabled": True, "stable": True}]}))
        os.environ["MODEL_REGISTRY_PATH"] = str(registry_path)
    logging.basicConfig(level=logging.WARNING)


def initialize_flash_database():
    """Initialize ONLY the named, isolated benchmark database; never alter live data."""
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from alembic import command

    target = make_url(os.environ["DATABASE_URL"])
    if target.database != FLASH_DATABASE or os.environ["EMBEDDING_DIMENSIONS"] != "1024":
        raise RuntimeError("Initialization is restricted to the isolated flash fixture database")
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": FLASH_DATABASE}
        )
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{FLASH_DATABASE}"'))
    admin.dispose()
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    engine = create_engine(target)
    with engine.begin() as connection:
        actual = connection.scalar(
            text(
                "SELECT format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid='chunk_search_index'::regclass AND attname='embedding' AND NOT attisdropped"
            )
        )
        if actual != "vector(1024)":
            # The lock closes the count/DDL race; populated databases are rejected.
            connection.execute(text("LOCK TABLE chunk_search_index IN ACCESS EXCLUSIVE MODE"))
            if connection.scalar(text("SELECT count(*) FROM chunk_search_index")):
                raise RuntimeError("Refusing to change a populated vector table")
            connection.execute(text("DROP INDEX IF EXISTS ix_chunk_search_embedding_hnsw"))
            connection.execute(text("ALTER TABLE chunk_search_index DROP COLUMN embedding"))
            connection.execute(
                text("ALTER TABLE chunk_search_index ADD COLUMN embedding vector(1024)")
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_chunk_search_embedding_hnsw ON chunk_search_index USING hnsw (embedding vector_cosine_ops) WITH (m=16,ef_construction=128) WHERE embedding IS NOT NULL"
                )
            )
    engine.dispose()
    container().index.ensure_index()
    print(json.dumps({"database": FLASH_DATABASE, "embedding_type": "vector(1024)", "ready": True}))


def container():
    from app.core.config import get_settings
    from app.core.container import ApplicationContainer

    if not hasattr(LOCAL, "app"):
        LOCAL.app = ApplicationContainer(get_settings())
        LOCAL.app.index.ensure_index()
    return LOCAL.app


def ingest_one(spec, project):
    from sqlalchemy import text

    from app.application.document_service import UploadDocumentCommand
    from app.db import SessionLocal
    from app.db.models import DocumentVersion

    app = container()
    start = time.perf_counter()
    with SessionLocal() as db, (OUTPUT / "documents" / spec["filename"]).open("rb") as stream:
        result = app.document_application.upload(
            db,
            UploadDocumentCommand(
                stream=stream,
                filename=spec["filename"],
                project=project,
                document_type=spec["role"],
                lifecycle_status=spec["lifecycle"],
                logical_key=spec["filename"],
                version_label="stress50-v1",
                owner="Stress50 evaluation",
            ),
        )
        if result.response.duplicate and not result.inline_job_id:
            # Rebuild ONLY this fixture version if the selected model changed.
            missing = db.scalar(
                text(
                    "SELECT count(*) FROM chunks c LEFT JOIN chunk_search_index s ON s.chunk_id=c.id WHERE c.version_id=:vid AND (s.embedding IS NULL OR s.embedding_fingerprint IS DISTINCT FROM :fp)"
                ),
                {"vid": result.response.version_id, "fp": app.settings.embedding_fingerprint},
            )
            if missing:
                job = app.document_repository.create_job(
                    db, result.response.document_id, result.response.version_id
                )
                db.commit()
                app.document_application.process_ingestion_job(db, job.id)
                result.response.job_id = job.id
        if result.inline_job_id:
            app.document_application.process_ingestion_job(db, result.inline_job_id)
        db.expire_all()
        job = app.document_application.get_job(db, result.response.job_id)
        version = db.get(DocumentVersion, result.response.version_id)
        return {
            "filename": spec["filename"],
            **result.response.model_dump(),
            "status": job.status,
            "technical_status": version.technical_status,
            "warnings": job.warnings,
            "elapsed_seconds": time.perf_counter() - start,
        }


def check_value(value, answer):
    answer = re.sub(r"(?<=\d),(?=\d)", "", answer)
    if value.isdigit():
        return bool(re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", answer))
    return value.casefold() in answer.casefold()


def answer_one(case, project_id, all_projects=False):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.db.models import Chunk, Document, DocumentVersion, QueryTrace

    app = container()
    start = time.perf_counter()
    with SessionLocal() as db:
        response = app.chat_application.answer(
            db,
            question=case["question"],
            project_ids=[] if all_projects else [project_id],
            conversation_id=None,
            pinned_model=os.environ.get("STRESS50_CHAT_MODEL", "qwen3.7-max-2026-06-08"),
        )
        trace = db.scalar(select(QueryTrace).where(QueryTrace.trace_id == response.trace_id))
        ids = [x["chunk_id"] for x in trace.retrieval_json.get("evidence", [])]
        rows = (
            db.execute(
                select(Chunk.id, Chunk.content, Chunk.heading_path, Document.filename)
                .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Chunk.id.in_(ids))
            ).all()
            if ids
            else []
        )
        by_id = {r[0]: r for r in rows}
        evidence = [by_id[c] for c in ids if c in by_id]
        expected = case["expected_files"]
        top_files = {r[3] for r in evidence[:10]}
        citations = {x.filename for x in response.sources}
        raw_text = "\n".join(r[1] for r in evidence)
        heading = case.get("heading")
        checks = {
            "status": response.status == case["status"],
            "answer_values": all(check_value(v, response.answer) for v in case["values"]),
            "required_citations": set(expected).issubset(citations),
            "scope": not case.get("scope") or response.resolved_scope == case["scope"],
            "no_scope_leak": not case.get("scope") or all(r[3] == case["scope"] for r in evidence),
        }
        return {
            "id": case["id"],
            "kind": case["kind"],
            "question": case["question"],
            "expected_status": case["status"],
            "expected_files": expected,
            "expected_values": case["values"],
            "pass": all(checks.values()),
            "checks": checks,
            "retrieval_all_files_at_10": set(expected).issubset(top_files) if expected else None,
            "retrieval_all_values": all(check_value(v, raw_text) for v in case["values"])
            if case["values"]
            else None,
            "heading_hit_at_5": any(
                heading.casefold() in (r[2] or "").casefold() for r in evidence[:5]
            )
            if heading
            else None,
            "retrieval": trace.retrieval_json,
            "evidence": [
                {"chunk_id": r[0], "heading": r[2], "filename": r[3], "content": r[1]}
                for r in evidence
            ],
            "response": response.model_dump(),
            "elapsed_seconds": time.perf_counter() - start,
            "project_scope": "all_authorized" if all_projects else "stress50_project",
        }


def run_jobs(items, fn, path, workers, resume):
    done = []
    if resume and path.exists():
        done = [json.loads(line) for line in path.read_text().splitlines() if line]
    key = "id" if items and "id" in items[0] else "filename"
    successful = {
        row[key]
        for row in done
        if not row.get("error") and row.get("status") not in {"failed", "cancelled"}
    }
    pending = [x for x in items if x[key] not in successful]
    mode = "a" if resume else "w"
    with path.open(mode, encoding="utf-8") as out, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {key: item[key], "error": type(exc).__name__, "pass": False}
                logging.error("Case %s failed: %s", item[key], type(exc).__name__)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            done.append(row)
            print(
                json.dumps(
                    {
                        "completed": len(done),
                        "total": len(items),
                        key: row[key],
                        "status": row.get("status"),
                        "pass": row.get("pass"),
                        "error": row.get("error"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return done


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["init-flash", "ingest", "answers", "audit"])
    p.add_argument("--chat-model", default="qwen3.7-max-2026-06-08")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--ids")
    p.add_argument("--all-projects", action="store_true")
    p.add_argument("--report", default="answers.jsonl")
    p.add_argument("--embedding-model", default="qwen3.7-text-embedding-flash")
    p.add_argument(
        "--connection-doc",
        type=Path,
        default=Path("/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md"),
    )
    p.add_argument(
        "--model-env", type=Path, default=Path("/Users/cliff/Documents/TellerxChatBot/.env")
    )
    p.add_argument(
        "--key-file",
        type=Path,
        default=Path("/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt"),
    )
    args = p.parse_args()
    configure(args)
    if args.mode == "init-flash":
        initialize_flash_database()
        return
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    from sqlalchemy import text

    from app.db import SessionLocal

    if args.mode == "ingest":
        # Create the dedicated project before concurrent uploads.
        with SessionLocal() as db:
            app = container()
            if not app.document_repository.get_project_by_name(db, manifest["project"]):
                app.document_application.create_project(db, manifest["project"])
        run_jobs(
            manifest["documents"],
            lambda s: ingest_one(s, manifest["project"]),
            OUTPUT / "ingestion.jsonl",
            args.workers,
            args.resume,
        )
    else:
        with SessionLocal() as db:
            pid = db.scalar(
                text("select id from projects where name=:name"), {"name": manifest["project"]}
            )
            if not pid:
                raise RuntimeError("Run ingestion before evaluation")
            if args.mode == "audit":
                params = {"pid": pid}
                sql = "FROM documents d JOIN document_versions v ON v.document_id=d.id JOIN chunks c ON c.version_id=v.id LEFT JOIN chunk_search_index s ON s.chunk_id=c.id WHERE d.project_id=:pid AND v.is_current"
                counts = dict(
                    db.execute(
                        text(
                            "SELECT count(distinct d.id) documents,count(distinct v.id) versions,count(distinct c.id) chunks,count(s.chunk_id) indexed,count(s.embedding) embedded "
                            + sql
                        ),
                        params,
                    )
                    .mappings()
                    .one()
                )
                counts["sections"] = db.scalar(
                    text(
                        "SELECT count(*) FROM document_sections ds JOIN document_versions v ON v.id=ds.version_id JOIN documents d ON d.id=v.document_id WHERE d.project_id=:pid AND ds.level>0"
                    ),
                    params,
                )
                counts["project_id"] = pid
                print(json.dumps(counts))
                (OUTPUT / "database-audit.json").write_text(json.dumps(counts, indent=2) + "\n")
                return
        cases = json.loads((OUTPUT / "questions.json").read_text())
        if args.ids:
            cases = [c for c in cases if c["id"] in args.ids.split(",")]
        if args.limit:
            cases = cases[: args.limit]
        run_jobs(
            cases,
            lambda q: answer_one(q, pid, args.all_projects),
            OUTPUT / args.report,
            args.workers,
            args.resume,
        )


if __name__ == "__main__":
    main()
