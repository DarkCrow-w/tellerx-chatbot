"""CLI facade for deterministic retrieval and grounded-answer benchmarks."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from evaluation.benchmark.corpus import CorpusDocument, generate_corpus
from evaluation.benchmark.evaluators import (
    _resolve_project_ids as _resolve_project_ids_impl,
)
from evaluation.benchmark.evaluators import (
    evaluate_answers,
    evaluate_offline_answer_pipeline,
    evaluate_offline_hybrid_retrieval,
    evaluate_project_filtering_offline,
    evaluate_retrieval,
)
from evaluation.benchmark.loaders import (
    evaluate_api_smoke_offline,
    index_existing,
    index_offline_hybrid,
    load_corpus,
    load_via_production_pipeline_offline,
)
from evaluation.benchmark.metrics import _answer_contains, _retrieval_acceptance
from evaluation.benchmark.offline import (
    EvidenceBoundBenchmarkRouter,
    OfflineBenchmarkQwen,
    OfflineHybridQwen,
    _offline_feature_vector,
)

__all__ = [
    "CorpusDocument",
    "EvidenceBoundBenchmarkRouter",
    "OfflineBenchmarkQwen",
    "OfflineHybridQwen",
    "_answer_contains",
    "_offline_feature_vector",
    "_resolve_project_ids",
    "_retrieval_acceptance",
    "evaluate_answers",
    "evaluate_api_smoke_offline",
    "evaluate_offline_answer_pipeline",
    "evaluate_offline_hybrid_retrieval",
    "evaluate_project_filtering_offline",
    "evaluate_retrieval",
    "generate_corpus",
    "index_existing",
    "index_offline_hybrid",
    "load_corpus",
    "load_via_production_pipeline_offline",
    "main",
]


def _resolve_project_ids(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Compatibility wrapper that keeps the historical patch point available."""
    return _resolve_project_ids_impl(rows, session_factory=SessionLocal)


def _print(report: dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and benchmark a deterministic 1K-document RAG corpus"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate")
    generate.add_argument("--output", type=Path, default=Path("evaluation/generated/benchmark-1k"))
    generate.add_argument("--count", type=int, default=1000)
    generate.add_argument("--questions", type=int, default=200)
    generate.add_argument("--seed", type=int, default=20260812)
    generate.add_argument("--force", action="store_true")

    load = sub.add_parser("load")
    load.add_argument("corpus", type=Path)
    load.add_argument("--reset", action="store_true")
    load.add_argument("--no-embedding", action="store_true")

    index_command = sub.add_parser("index-existing")
    index_command.add_argument("corpus", type=Path)
    index_command.add_argument("--no-embedding", action="store_true")

    retrieve = sub.add_parser("retrieve")
    retrieve.add_argument("corpus", type=Path)
    retrieve.add_argument("--limit", type=int)
    retrieve.add_argument("--no-rerank", action="store_true")
    retrieve.add_argument("--no-vector", action="store_true")

    answers = sub.add_parser("answers")
    answers.add_argument("corpus", type=Path)
    answers.add_argument("--limit", type=int, default=20)
    answers.add_argument("--model", default="qwen3.7-plus-2026-05-26")

    offline_answers = sub.add_parser("answers-offline")
    offline_answers.add_argument("corpus", type=Path)

    offline_hybrid = sub.add_parser("hybrid-offline")
    offline_hybrid.add_argument("corpus", type=Path)

    production_load = sub.add_parser("load-production-offline")
    production_load.add_argument("corpus", type=Path)
    production_load.add_argument("--reset", action="store_true")

    api_smoke = sub.add_parser("api-smoke-offline")
    api_smoke.add_argument("corpus", type=Path)

    project_filter = sub.add_parser("project-filter-offline")
    project_filter.add_argument("corpus", type=Path)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args()
    if args.command == "generate":
        _print(generate_corpus(args.output, args.count, args.questions, args.seed, args.force))
    elif args.command == "load":
        _print(load_corpus(args.corpus, reset=args.reset, embedding=not args.no_embedding))
    elif args.command == "index-existing":
        _print(index_existing(args.corpus, embedding=not args.no_embedding))
    elif args.command == "retrieve":
        report = evaluate_retrieval(
            args.corpus,
            limit=args.limit,
            use_rerank=not args.no_rerank,
            use_vector=not args.no_vector,
        )
        _print(report)
        if not report["acceptance"]["passed"]:
            raise SystemExit(2)
    elif args.command == "answers":
        report = evaluate_answers(args.corpus, limit=args.limit, model=args.model)
        _print(report)
        if not report["acceptance"]["passed"]:
            raise SystemExit(2)
    elif args.command == "answers-offline":
        report = evaluate_offline_answer_pipeline(args.corpus)
        _print(report)
        if not report["acceptance"]["passed"]:
            raise SystemExit(2)
    elif args.command == "hybrid-offline":
        _print(index_offline_hybrid(args.corpus))
        report = evaluate_offline_hybrid_retrieval(args.corpus)
        _print(report)
        if not report["acceptance"]["passed"]:
            raise SystemExit(2)
    elif args.command == "load-production-offline":
        report = load_via_production_pipeline_offline(args.corpus, reset=args.reset)
        _print(report)
        if not report["acceptance"]["passed"]:
            raise SystemExit(2)
    elif args.command == "api-smoke-offline":
        report = evaluate_api_smoke_offline(args.corpus)
        _print(report)
        if not report["passed"]:
            raise SystemExit(2)
    else:
        report = evaluate_project_filtering_offline(args.corpus)
        _print(report)
        if not report["passed"]:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
