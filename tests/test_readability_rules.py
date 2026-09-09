"""Behavioral boundaries for the readability refactor."""
from itertools import product
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.repositories.chat import ChatRepository
from app.services.candidate_selection import prefer_complete_entity_matches
from app.services.retrieval import Retriever


def candidate(document_id, content):
    return {"hit": {"_source": {"document_id": document_id, "content": content}}}


def test_live_chunks_require_searchability_and_valid_lifecycle():
    rows = []
    expected = set()
    for lifecycle, technical, current, deleted in product(
        ["draft", "approved", "deprecated"], ["searchable", "pending"], [True, False], [True, False]
    ):
        chunk_id = str(len(rows))
        rows.append((chunk_id, lifecycle, technical, current, deleted))
        searchable = not deleted and technical == "searchable"
        valid_version = lifecycle == "draft" or (lifecycle == "approved" and current)
        if searchable and valid_version:
            expected.add(chunk_id)
    db = Mock()
    db.execute.return_value.all.return_value = rows
    assert ChatRepository().live_searchable_chunk_ids(db, {row[0] for row in rows}) == expected


def test_entity_filter_preserves_order_document_inheritance_and_links():
    rows = [candidate("other", "unrelated"), candidate("a", "Alpha subject"),
            candidate("a", "inherited"), candidate("b", "LINK-1"), candidate("", "unrelated")]
    assert prefer_complete_entity_matches(
        "question", rows, ["link-1"], subject_signals=("alpha",),
    ) == rows[1:4]
    assert prefer_complete_entity_matches("question", rows, subject_signals=("missing",)) is rows
    assert prefer_complete_entity_matches("question", rows, subject_signals=()) is rows


@pytest.mark.parametrize("queries,expected", [((), []), (("rewrite", "question", "", "other", "fifth"), ["rewrite", "other"])])
def test_query_plan_controls_rewrites_even_when_empty(queries, expected):
    retriever = Mock()
    retriever._retrieve_for_statuses.return_value = []
    plan = SimpleNamespace(subjects=("fallback",), retrieval_queries=queries)
    assert Retriever._recall_planned_queries(retriever, "question", [], None, plan, None, []) == []
    assert [call.args[0] for call in retriever._retrieve_for_statuses.call_args_list] == expected


@pytest.mark.parametrize("signals,expected", [([], []), (["focus"], ["focus"])])
def test_queries_without_plan_use_subject_fallback(signals, expected):
    retriever = Mock()
    retriever._retrieve_for_statuses.return_value = []
    with patch("app.services.retrieval._query_subject_signals", return_value=signals):
        Retriever._recall_planned_queries(retriever, "question", [], None, None, None, [])
    assert [call.args[0] for call in retriever._retrieve_for_statuses.call_args_list] == expected


@pytest.mark.parametrize("enabled,has_plan", product([True, False], repeat=2))
def test_search_preserves_anchor_source_and_ranking_inputs(enabled, has_plan):
    rows = [candidate("doc", "alpha")]
    retriever = Mock()
    retriever.settings = SimpleNamespace(
        business_retrieval_enabled=enabled, rerank_candidates=10, evidence_top_k=5,
    )
    retriever._retrieve_for_statuses.return_value = rows
    retriever._recall_planned_queries.return_value = rows
    retriever._business_channel.return_value = (rows, ("business",), ["doc"])
    retriever._prioritize_facts.return_value = rows
    retriever._related_evidence.return_value = []
    retriever._rank_candidates.return_value = ["evidence"]
    plan = None
    if has_plan:
        plan = SimpleNamespace(anchor_signals=("planned",), rerank_context=lambda: "context")
    with (
        patch("app.services.retrieval.business_queries.subject_signals", return_value=("alpha",)),
        patch("app.services.retrieval.candidate_ranking.boost_anchor_matches", return_value=rows) as boost,
        patch("app.services.retrieval.candidate_selection.discover_linked_identifiers", return_value=[]),
        patch("app.services.retrieval.candidate_ranking.deduplicate_content", return_value=rows),
        patch("app.services.retrieval.candidate_selection.enforce_exact_identifiers", return_value=rows),
        patch("app.services.retrieval.candidate_selection.select_rerank_candidate_pool", return_value=rows),
    ):
        result = Retriever._search_once(retriever, "question", ["project"], None, plan, document_ids=["doc"])
    expected_anchors = ("business",) if enabled else (("planned",) if has_plan else ())
    assert result == ["evidence"]
    boost.assert_called_once_with(rows, expected_anchors)
    assert retriever._rank_candidates.call_args.kwargs["semantic_anchors"] == expected_anchors
    assert retriever._business_channel.call_count == int(enabled)
