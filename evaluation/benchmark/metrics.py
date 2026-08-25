"""Shared benchmark scoring and latency helpers."""

from __future__ import annotations

import math
import re
import statistics
from typing import Any


def _latency_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0, "p50": 0, "p95": 0, "max": 0}
    ordered = sorted(values)
    return {
        "mean": round(statistics.fmean(values), 3),
        "p50": round(statistics.median(values), 3),
        "p95": round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)], 3),
        "max": round(max(values), 3),
    }


def _answer_contains(answer: str, expected: object) -> bool:
    """Compare answer facts while ignoring numeric thousands separators."""
    haystack = answer.casefold()
    needle = str(expected).casefold()
    if needle in haystack:
        return True
    compact_haystack = re.sub(r"(?<=\d)[,_\s](?=\d)", "", haystack)
    compact_needle = re.sub(r"(?<=\d)[,_\s](?=\d)", "", needle)
    return compact_needle in compact_haystack


def _retrieval_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "recall_at_10_gte_90pct": report["recall_at_10"] >= 0.90,
        "unanswerable_abstention_gte_90pct": (
            report["unanswerable_abstention_rate"] is None
            or report["unanswerable_abstention_rate"] >= 0.90
        ),
        "excel_location_gte_90pct": (
            report["excel_location_accuracy"] is None or report["excel_location_accuracy"] >= 0.90
        ),
        "answer_fact_coverage_gte_95pct": (
            report.get("answer_fact_coverage") is None or report["answer_fact_coverage"] >= 0.95
        ),
        "local_p95_lte_2000ms": report["latency_ms"]["p95"] <= 2000,
    }
    if report["mode"]["rerank_requested"]:
        checks["rerank_effective"] = report["mode"]["rerank_effective"]
    return {"passed": all(checks.values()), "checks": checks}
