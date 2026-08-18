from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import SessionLocal
from app.dependencies import answer_service, model_registry, qwen_client
from app.qwen import QwenAPIError


def _safe_error(component: str, exc: QwenAPIError, **fields: object) -> str:
    """Expose actionable API metadata without logging credentials or response text."""
    return json.dumps(
        {
            "component": component,
            "status": "failed",
            **fields,
            "http_status": exc.status_code,
            "error_code": exc.code,
        },
        ensure_ascii=False,
    )


def diagnostics_main() -> None:
    parser = argparse.ArgumentParser(description="Run minimal, explicit Qwen API diagnostics")
    parser.add_argument("--chat-model", default="qwen3.7-plus-2026-05-26")
    parser.add_argument("--skip-chat", action="store_true")
    parser.add_argument("--skip-embedding", action="store_true")
    parser.add_argument("--skip-rerank", action="store_true")
    args = parser.parse_args()
    client = qwen_client()
    failures = 0
    if not args.skip_embedding:
        try:
            vectors, usage = client.embeddings(["企业知识库 API connectivity check"])
            print(json.dumps({"component": "embedding", "status": "ok", "dimension": len(vectors[0]), "tokens": usage.total_tokens}, ensure_ascii=False))
        except QwenAPIError as exc:
            failures += 1
            print(_safe_error("embedding", exc))
    if not args.skip_rerank:
        try:
            ranked = client.rerank("API check", ["API connectivity check", "unrelated text"], 1)
            print(json.dumps({"component": "rerank", "status": "ok", "results": len(ranked)}))
        except QwenAPIError as exc:
            failures += 1
            print(_safe_error("rerank", exc))
    if not args.skip_chat:
        try:
            result = client.chat_json(
                model_id=args.chat_model,
                system_prompt="Return JSON only.",
                user_prompt='Return {"status":"ok"}.',
                max_tokens=30,
            )
            print(json.dumps({"component": "chat", "status": "ok", "model": result.model_id, "tokens": result.usage.total_tokens}, ensure_ascii=False))
        except QwenAPIError as exc:
            failures += 1
            print(_safe_error("chat", exc, model=args.chat_model))
    raise SystemExit(1 if failures else 0)


def evaluate_main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval and grounded answers from a JSONL set")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, default=Path("evaluation-results.jsonl"))
    parser.add_argument("--model", help="Pin one configured model; disables automatic model fallback")
    args = parser.parse_args()
    if args.model and not model_registry().by_id(args.model):
        parser.error(f"Unknown model: {args.model}")
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = []
    with SessionLocal() as db:
        for row in rows:
            response = answer_service().answer(
                db,
                question=row["question"],
                project_ids=row.get("project_ids", []),
                conversation_id=None,
                pinned_model=args.model,
            )
            cited_documents = sorted({source.document_id for source in response.sources})
            expected_documents = sorted(row.get("expected_document_ids", []))
            results.append(
                {
                    "id": row.get("id"),
                    "question": row["question"],
                    "status": response.status,
                    "answer": response.answer,
                    "model_id": response.model_id,
                    "cited_document_ids": cited_documents,
                    "expected_document_ids": expected_documents,
                    "document_hit": bool(set(cited_documents) & set(expected_documents)) if expected_documents else None,
                    "trace_id": response.trace_id,
                }
            )
    args.output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in results) + "\n", encoding="utf-8")
    evaluated = [row for row in results if row["document_hit"] is not None]
    recall = sum(bool(row["document_hit"]) for row in evaluated) / len(evaluated) if evaluated else 0
    print(json.dumps({"questions": len(results), "document_hit_rate": recall, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    diagnostics_main()
