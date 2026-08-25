"""Run labelled business-question evaluations against the production answer service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.container import answer_service, model_registry
from app.db import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval and grounded answers from a JSONL set"
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, default=Path("evaluation-results.jsonl"))
    parser.add_argument(
        "--model", help="Pin one configured model; disables automatic model fallback"
    )
    args = parser.parse_args()
    if args.model and not model_registry().by_id(args.model):
        parser.error(f"Unknown model: {args.model}")
    rows = [
        json.loads(line)
        for line in args.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
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
                    "document_hit": (
                        bool(set(cited_documents) & set(expected_documents))
                        if expected_documents
                        else None
                    ),
                    "trace_id": response.trace_id,
                }
            )
    args.output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in results) + "\n",
        encoding="utf-8",
    )
    evaluated = [row for row in results if row["document_hit"] is not None]
    recall = (
        sum(bool(row["document_hit"]) for row in evaluated) / len(evaluated) if evaluated else 0
    )
    print(
        json.dumps(
            {
                "questions": len(results),
                "document_hit_rate": recall,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
