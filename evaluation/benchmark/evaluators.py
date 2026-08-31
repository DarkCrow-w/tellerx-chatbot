"""Retrieval and grounded-answer evaluators for generated benchmark corpora."""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.container import answer_service, model_client, search_index
from app.db import Base, SessionLocal
from app.db.models import Project
from app.integrations.openai_client import ModelAPIError
from app.integrations.search import SearchIndex
from app.services.answering import AnswerService
from app.services.retrieval import Retriever
from evaluation.benchmark.corpus import _read_jsonl
from evaluation.benchmark.metrics import (
    _answer_contains,
    _latency_stats,
    _retrieval_acceptance,
)
from evaluation.benchmark.offline import (
    EvidenceBoundBenchmarkRouter,
    OfflineBenchmarkQwen,
    OfflineHybridQwen,
)

logger = logging.getLogger(__name__)


def _resolve_project_ids(
    rows: list[dict[str, Any]],
    *,
    session_factory: Callable[[], Session] | None = None,
) -> dict[str, str]:
    """Resolve corpus project names once and fail closed without isolation."""

    required = {str(row["project"]) for row in rows if row.get("project")}
    if not required:
        return {}
    factory = session_factory or SessionLocal
    with factory() as db:
        resolved = {
            project.name: project.id
            for project in db.scalars(select(Project).where(Project.name.in_(required)))
        }
    missing = sorted(required - resolved.keys())
    if missing:
        raise ValueError("Benchmark projects are not loaded: " + ", ".join(missing))
    return resolved


def evaluate_retrieval(
    corpus_dir: Path,
    *,
    limit: int | None,
    use_rerank: bool,
    use_vector: bool,
) -> dict[str, Any]:
    settings = Settings(allow_bm25_only=not use_vector)
    index = search_index()
    qwen = model_client()
    retriever = Retriever(settings, index, qwen)
    questions = _read_jsonl(corpus_dir / "questions.jsonl")
    if limit:
        answerable = [row for row in questions if row["expected_status"] == "answered"][:limit]
        missing = [row for row in questions if row["expected_status"] != "answered"][
            : max(5, limit // 10)
        ]
        questions = [*answerable, *missing]
    project_ids = _resolve_project_ids(questions)

    original_embedding = retriever._query_embedding
    original_rerank = qwen.rerank
    rerank_successes = 0
    rerank_failures = 0
    if not use_vector:

        def disabled_embedding(_: str) -> list[float]:
            raise RuntimeError("disabled for lexical benchmark")

        retriever._query_embedding = disabled_embedding  # type: ignore[method-assign]
    if not use_rerank:

        def disabled_rerank(*_: Any, **__: Any) -> list[tuple[int, float]]:
            raise ModelAPIError("disabled for RRF benchmark", code="benchmark_disabled")

        qwen.rerank = disabled_rerank  # type: ignore[method-assign]
    else:

        def measured_rerank(*args: Any, **kwargs: Any) -> list[tuple[int, float]]:
            nonlocal rerank_successes, rerank_failures
            try:
                result = original_rerank(*args, **kwargs)
                rerank_successes += 1
                return result
            except ModelAPIError:
                rerank_failures += 1
                raise

        qwen.rerank = measured_rerank  # type: ignore[method-assign]

    latencies = []
    outcomes = []
    hits_at_1 = hits_at_5 = hits_at_10 = 0
    reciprocal_ranks = []
    correct_abstentions = 0
    approved_precedence_total = 0
    approved_precedence_pass = 0
    location_total = 0
    location_pass = 0
    fact_coverage_total = 0
    fact_coverage_pass = 0
    for position, row in enumerate(questions, start=1):
        started = time.perf_counter()
        project_filter = [project_ids[row["project"]]] if row.get("project") else []
        evidence = retriever.search(row["question"], project_filter)
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        filenames = [item.filename for item in evidence]
        expected = row.get("expected_filename")
        expected_filenames = row.get("expected_filenames", [expected] if expected else [])
        expected_ranks = [
            filenames.index(filename) + 1 if filename in filenames else None
            for filename in expected_filenames
        ]
        rank = max(expected_ranks) if expected_ranks and all(expected_ranks) else None
        if rank:
            hits_at_1 += rank <= 1
            hits_at_5 += rank <= 5
            hits_at_10 += rank <= 10
            reciprocal_ranks.append(1 / rank)
        elif expected:
            reciprocal_ranks.append(0)
        if row["expected_status"] == "insufficient_evidence" and not evidence:
            correct_abstentions += 1
        if row.get("expected_version_label") and any(
            item.filename == expected for item in evidence
        ):
            approved_precedence_total += 1
            first_expected = next(item for item in evidence if item.filename == expected)
            approved_precedence_pass += (
                first_expected.version_label == row["expected_version_label"]
            )
        if row.get("expected_sheet"):
            location_total += 1
            location_pass += any(
                item.filename in expected_filenames
                and item.sheet_name == row["expected_sheet"]
                and item.cell_range == row["expected_cell_range"]
                for item in evidence
            )
        facts_covered = None
        if row["expected_status"] == "answered":
            fact_coverage_total += 1
            evidence_text = "\n".join(item.content for item in evidence)
            facts_covered = all(
                _answer_contains(evidence_text, value) for value in row.get("answer_contains", [])
            )
            fact_coverage_pass += facts_covered
        outcomes.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "expected_filename": expected,
                "rank": rank,
                "returned": filenames,
                "facts_covered": facts_covered,
                "latency_ms": round(latency, 3),
            }
        )
        if position % 25 == 0:
            logger.info("Evaluated %s/%s questions", position, len(questions))
    retriever._query_embedding = original_embedding  # type: ignore[method-assign]
    qwen.rerank = original_rerank  # type: ignore[method-assign]
    answerable_count = sum(row["expected_status"] == "answered" for row in questions)
    missing_count = len(questions) - answerable_count
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for result in outcomes:
        by_kind[result["kind"]].append(result)
    report = {
        "mode": {
            "vector_requested": use_vector,
            "rerank_requested": use_rerank,
            "rerank_success_calls": rerank_successes,
            "rerank_failed_calls": rerank_failures,
            "rerank_effective": bool(use_rerank and rerank_successes and not rerank_failures),
        },
        "questions": len(questions),
        "answerable_questions": answerable_count,
        "unanswerable_questions": missing_count,
        "recall_at_1": round(hits_at_1 / answerable_count, 4) if answerable_count else 0,
        "recall_at_5": round(hits_at_5 / answerable_count, 4) if answerable_count else 0,
        "recall_at_10": round(hits_at_10 / answerable_count, 4) if answerable_count else 0,
        "mrr": round(statistics.fmean(reciprocal_ranks), 4) if reciprocal_ranks else 0,
        "unanswerable_abstention_rate": round(correct_abstentions / missing_count, 4)
        if missing_count
        else None,
        "approved_precedence_rate": round(approved_precedence_pass / approved_precedence_total, 4)
        if approved_precedence_total
        else None,
        "excel_location_accuracy": round(location_pass / location_total, 4)
        if location_total
        else None,
        "answer_fact_coverage": round(fact_coverage_pass / fact_coverage_total, 4)
        if fact_coverage_total
        else None,
        "latency_ms": _latency_stats(latencies),
        "by_kind": {
            kind: {
                "count": len(rows),
                "recall_at_5": round(
                    sum(row["rank"] is not None and row["rank"] <= 5 for row in rows) / len(rows), 4
                ),
            }
            for kind, rows in by_kind.items()
        },
        "failures": [
            row
            for row in outcomes
            if (row["expected_filename"] and row["rank"] is None) or row["facts_covered"] is False
        ],
    }
    report["acceptance"] = _retrieval_acceptance(report)
    suffix = f"retrieval-v{'1' if use_vector else '0'}-r{'1' if use_rerank else '0'}"
    (corpus_dir / f"{suffix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def evaluate_offline_hybrid_retrieval(corpus_dir: Path) -> dict[str, Any]:
    settings = Settings(embedding_model="offline-feature-hash-v1", allow_bm25_only=False)
    index = SearchIndex(settings)
    qwen = OfflineHybridQwen()
    retriever = Retriever(settings, index, qwen)  # type: ignore[arg-type]
    questions = _read_jsonl(corpus_dir / "questions.jsonl")
    latencies = []
    outcomes = []
    correct_abstentions = 0
    for position, row in enumerate(questions, start=1):
        started = time.perf_counter()
        evidence = retriever.search(row["question"], [])
        latencies.append((time.perf_counter() - started) * 1000)
        filenames = [item.filename for item in evidence]
        expected_filenames = row.get("expected_filenames", [])
        expected_ranks = [
            filenames.index(filename) + 1 if filename in filenames else None
            for filename in expected_filenames
        ]
        rank = max(expected_ranks) if expected_ranks and all(expected_ranks) else None
        if row["expected_status"] == "insufficient_evidence" and not evidence:
            correct_abstentions += 1
        outcomes.append({"id": row["id"], "kind": row["kind"], "rank": rank})
        if position % 25 == 0:
            logger.info("Evaluated offline hybrid retrieval %s/%s", position, len(questions))
    answerable = [row for row in outcomes if not row["id"].startswith("q-missing-")]
    missing_count = len(outcomes) - len(answerable)
    report = {
        "mode": "offline_feature_hash_knn_rrf_rerank",
        "disclaimer": "Validates hybrid mechanics; not Qwen embedding/rerank quality.",
        "questions": len(outcomes),
        "recall_at_1": round(sum(row["rank"] == 1 for row in answerable) / len(answerable), 4),
        "recall_at_5": round(
            sum(row["rank"] is not None and row["rank"] <= 5 for row in answerable)
            / len(answerable),
            4,
        ),
        "recall_at_10": round(
            sum(row["rank"] is not None and row["rank"] <= 10 for row in answerable)
            / len(answerable),
            4,
        ),
        "unanswerable_abstention_rate": round(correct_abstentions / missing_count, 4),
        "latency_ms": _latency_stats(latencies),
        "failures": [row for row in answerable if row["rank"] is None],
    }
    report["acceptance"] = {
        "passed": report["recall_at_10"] >= 0.90 and report["unanswerable_abstention_rate"] >= 0.90,
        "checks": {
            "recall_at_10_gte_90pct": report["recall_at_10"] >= 0.90,
            "unanswerable_abstention_gte_90pct": (report["unanswerable_abstention_rate"] >= 0.90),
        },
    }
    (corpus_dir / "retrieval-offline-hybrid.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def evaluate_project_filtering_offline(corpus_dir: Path) -> dict[str, Any]:
    settings = Settings(embedding_model="offline-production-pipeline", allow_bm25_only=True)
    retriever = Retriever(settings, SearchIndex(settings), OfflineBenchmarkQwen())  # type: ignore[arg-type]
    rows = [
        row
        for row in _read_jsonl(corpus_dir / "questions.jsonl")
        if row["expected_status"] == "answered" and row.get("project")
    ]
    with SessionLocal() as db:
        project_ids = {project.name: project.id for project in db.scalars(select(Project))}
    outcomes = []
    for row in rows:
        evidence = retriever.search(row["question"], [project_ids[row["project"]]])
        expected_filenames = set(row.get("expected_filenames", []))
        filenames = {item.filename for item in evidence}
        outcomes.append(
            {
                "id": row["id"],
                "expected_hit": expected_filenames.issubset(filenames),
                "all_evidence_in_project": all(
                    item.project_id == project_ids[row["project"]] for item in evidence
                ),
            }
        )
    report = {
        "mode": "project_filtering_offline",
        "questions": len(outcomes),
        "hit_accuracy": round(sum(row["expected_hit"] for row in outcomes) / len(outcomes), 4),
        "filter_isolation_accuracy": round(
            sum(row["all_evidence_in_project"] for row in outcomes) / len(outcomes), 4
        ),
        "failures": [
            row for row in outcomes if not row["expected_hit"] or not row["all_evidence_in_project"]
        ],
    }
    report["passed"] = not report["failures"]
    (corpus_dir / "project-filter-offline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def evaluate_answers(corpus_dir: Path, *, limit: int, model: str) -> dict[str, Any]:
    rows = _read_jsonl(corpus_dir / "questions.jsonl")
    answerable = [row for row in rows if row["expected_status"] == "answered"][:limit]
    missing = [row for row in rows if row["expected_status"] == "insufficient_evidence"][
        : max(3, limit // 5)
    ]
    selected = [*answerable, *missing]
    project_ids = _resolve_project_ids(selected)
    service: AnswerService = answer_service()
    results = []
    latencies = []
    status_correct = citation_correct = content_correct = 0
    with SessionLocal() as db:
        for row in selected:
            started = time.perf_counter()
            response = service.answer(
                db,
                question=row["question"],
                project_ids=[project_ids[row["project"]]] if row.get("project") else [],
                conversation_id=None,
                pinned_model=model,
            )
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            status_ok = response.status == row["expected_status"]
            expected_filenames = set(row.get("expected_filenames", []))
            source_filenames = {source.filename for source in response.sources}
            citations_ok = (
                expected_filenames.issubset(source_filenames)
                if expected_filenames
                else not response.sources and not response.claims
            )
            content_ok = all(
                _answer_contains(response.answer, value) for value in row["answer_contains"]
            )
            status_correct += status_ok
            citation_correct += citations_ok
            content_correct += content_ok
            results.append(
                {
                    "id": row["id"],
                    "question": row["question"],
                    "expected_status": row["expected_status"],
                    "actual_status": response.status,
                    "status_correct": status_ok,
                    "citation_correct": citations_ok,
                    "content_correct": content_ok,
                    "answer": response.answer,
                    "source_filenames": sorted(source_filenames),
                    "model": response.model_id,
                    "latency_ms": round(latency, 3),
                }
            )
    report = {
        "model": model,
        "questions": len(selected),
        "status_accuracy": round(status_correct / len(selected), 4),
        "citation_accuracy": round(citation_correct / len(selected), 4),
        "content_accuracy": round(content_correct / len(selected), 4),
        "latency_ms": _latency_stats(latencies),
        "results": results,
    }
    report["acceptance"] = {
        "passed": report["status_accuracy"] >= 0.90
        and report["citation_accuracy"] >= 0.95
        and report["content_accuracy"] >= 0.98,
        "checks": {
            "status_accuracy_gte_90pct": report["status_accuracy"] >= 0.90,
            "citation_accuracy_gte_95pct": report["citation_accuracy"] >= 0.95,
            "content_accuracy_gte_98pct": report["content_accuracy"] >= 0.98,
        },
    }
    model_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "unknown-model"
    report_path = corpus_dir / f"answer-report-{model_slug}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def evaluate_offline_answer_pipeline(corpus_dir: Path) -> dict[str, Any]:
    """Exercise grounded-answer invariants for the complete scale corpus."""
    rows = _read_jsonl(corpus_dir / "questions.jsonl")
    expected_by_question = {row["question"]: row for row in rows}
    settings = Settings(allow_bm25_only=True, validate_citations_against_database=False)
    offline_qwen = OfflineBenchmarkQwen()
    retriever = Retriever(settings, search_index(), offline_qwen)  # type: ignore[arg-type]
    router = EvidenceBoundBenchmarkRouter(expected_by_question)
    service = AnswerService(settings, retriever, router)  # type: ignore[arg-type]
    memory_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(memory_engine)

    outcomes = []
    latencies = []
    status_pass = citation_pass = content_pass = lifecycle_pass = 0
    with Session(memory_engine) as db:
        for position, row in enumerate(rows, start=1):
            started = time.perf_counter()
            response = service.answer(
                db,
                question=row["question"],
                project_ids=[],
                conversation_id=None,
                pinned_model=None,
            )
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            status_ok = response.status == row["expected_status"]
            expected_filenames = set(row.get("expected_filenames", []))
            if expected_filenames:
                source_filenames = {source.filename for source in response.sources}
                citation_ok = expected_filenames.issubset(
                    source_filenames
                ) and source_filenames.issubset(expected_filenames)
                lifecycle_ok = all(
                    source.document_status == "approved" for source in response.sources
                )
            else:
                citation_ok = not response.sources and not response.claims
                lifecycle_ok = not response.sources
            content_ok = all(
                _answer_contains(response.answer, value) for value in row["answer_contains"]
            )
            status_pass += status_ok
            citation_pass += citation_ok
            content_pass += content_ok
            lifecycle_pass += lifecycle_ok
            outcomes.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "status_ok": status_ok,
                    "citation_ok": citation_ok,
                    "content_ok": content_ok,
                    "lifecycle_ok": lifecycle_ok,
                    "actual_status": response.status,
                    "model": response.model_id,
                    "route_tier": response.route_tier,
                    "source_files": [source.filename for source in response.sources],
                    "latency_ms": round(latency, 3),
                }
            )
            if position % 25 == 0:
                logger.info("Evaluated offline answer pipeline %s/%s", position, len(rows))
    total = len(rows)
    report = {
        "mode": "offline_evidence_bound_pipeline",
        "disclaimer": "Validates answer orchestration and citation invariants, not Qwen quality.",
        "questions": total,
        "status_accuracy": round(status_pass / total, 4),
        "citation_accuracy": round(citation_pass / total, 4),
        "content_accuracy": round(content_pass / total, 4),
        "approved_only_accuracy": round(lifecycle_pass / total, 4),
        "route_tier_calls": dict(router.tier_calls),
        "latency_ms": _latency_stats(latencies),
        "failures": [
            outcome
            for outcome in outcomes
            if not all(
                outcome[key] for key in ("status_ok", "citation_ok", "content_ok", "lifecycle_ok")
            )
        ],
    }
    report["acceptance"] = {
        "passed": not report["failures"]
        and report["citation_accuracy"] >= 0.95
        and report["status_accuracy"] >= 0.90,
        "checks": {
            "status_accuracy_gte_90pct": report["status_accuracy"] >= 0.90,
            "citation_accuracy_gte_95pct": report["citation_accuracy"] >= 0.95,
            "approved_only_accuracy_100pct": report["approved_only_accuracy"] == 1.0,
            "no_case_failures": not report["failures"],
        },
    }
    (corpus_dir / "answer-pipeline-offline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
