from pathlib import Path

import pytest

from app.commands.benchmark import (
    _answer_contains,
    _offline_feature_vector,
    _retrieval_acceptance,
    generate_corpus,
)
from app.knowledge.parsers import DocumentParser


def test_answer_content_matching_ignores_numeric_thousands_separators() -> None:
    assert _answer_contains("门槛为 527,400 CNY", "527400")
    assert _answer_contains("编号 E-7101", "E-7101")
    assert not _answer_contains("门槛为 527,401 CNY", "527400")


def test_generates_exact_document_count_and_ground_truth(tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    summary = generate_corpus(output, count=30, questions=10, seed=7, force=False)
    manifest = (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    questions = (output / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    assert summary["documents"] == 30
    assert len(manifest) == 30
    # 10 single-document, 9 comparison, and 10 refusal questions.
    assert len(questions) == 29
    assert set(summary["formats"]) == {"md", "txt", "html", "docx", "xlsx", "pdf"}


def test_all_generated_formats_are_parseable(tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    generate_corpus(output, count=60, questions=5, seed=3, force=False)
    parser = DocumentParser(backend="native")
    suffixes = set()
    for path in (output / "documents").iterdir():
        units, _ = parser.parse(path)
        assert units
        assert any("KBR-" in unit.text for unit in units)
        suffixes.add(path.suffix)
    assert suffixes == {".md", ".txt", ".html", ".docx", ".xlsx", ".pdf"}


def test_retrieval_acceptance_rejects_silent_rerank_fallback() -> None:
    report = {
        "mode": {"rerank_requested": True, "rerank_effective": False},
        "recall_at_10": 1.0,
        "unanswerable_abstention_rate": 1.0,
        "excel_location_accuracy": 1.0,
        "latency_ms": {"p95": 10},
    }
    assert _retrieval_acceptance(report)["passed"] is False


def test_offline_feature_vector_is_deterministic_and_normalized() -> None:
    first = _offline_feature_vector("KBR-0001 琥珀猎鹰")
    second = _offline_feature_vector("KBR-0001 琥珀猎鹰")
    assert first == second
    assert len(first) == 1024
    assert sum(value * value for value in first) == pytest.approx(1.0)
