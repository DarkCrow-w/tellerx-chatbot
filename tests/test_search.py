from types import SimpleNamespace

from app.integrations.search import SearchIndex, _lexical_signals, lexical_tokens
from app.services.retrieval import Retriever


class FakeResult:
    rowcount = 0

    @staticmethod
    def fetchall():
        return []


class FakeConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult()


class FakeEngine:
    def __init__(self):
        self.connection = FakeConnection()

    def connect(self):
        return self.connection

    def begin(self):
        return self.connection


def _postgres_settings(**overrides):
    values = {
        "embedding_fingerprint": "model-1024-v1",
        "postgres_search_table": "chunk_search_index",
        "vector_min_similarity": 0.25,
        "pgvector_hnsw_ef_search": 200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mixed_language_lexicalization_keeps_ids_and_chinese_business_terms() -> None:
    tokens = lexical_tokens("翠湖授信 Greenlake Credit CTL-4616 审批门槛")
    assert "ctl-4616" in tokens
    assert "greenlake" in tokens
    assert "翠湖" in tokens
    assert "授信" in tokens


def test_postgres_document_text_persists_exact_business_identifiers() -> None:
    index = SearchIndex(_postgres_settings(), engine=FakeEngine())
    raw_text, lexical_text, exact_terms = index._document_text(
        {
            "filename": "rule.md",
            "title_path": "当前审批规则",
            "content": "翠湖授信由 CTL-4616 管控，运行时是 CORE-6216。",
        }
    )
    assert "翠湖授信" in raw_text
    assert "翠湖" in lexical_text
    assert exact_terms == ["core-6216", "ctl-4616"]


def test_postgres_document_text_preserves_filename_and_title_weight() -> None:
    index = SearchIndex(_postgres_settings(), engine=FakeEngine())
    _, lexical_text, _ = index._document_text(
        {
            "filename": "settlement.md",
            "title_path": "Approval Policy",
            "content": "queue timeout",
        }
    )
    tokens = lexical_text.split()
    assert tokens.count("settlement.md") == 4
    assert tokens.count("approval") == 3
    assert tokens.count("queue") == 1


def test_lexical_search_uses_safe_websearch_syntax_for_punctuated_ids() -> None:
    engine = FakeEngine()
    SearchIndex(_postgres_settings(), engine=engine).lexical_search(
        "请查 CTL-4616 / API:v2", ["project-1"], ["approved"], 5
    )
    sql, params = engine.connection.calls[-1]
    assert "websearch_to_tsquery" in sql
    assert "SELECT to_tsquery('simple'" not in sql
    assert '"ctl-4616"' in params["ts_query"]


def test_vector_search_uses_pgvector_and_pre_filters() -> None:
    engine = FakeEngine()
    SearchIndex(_postgres_settings(), engine=engine).vector_search(
        [0.1], ["project-1"], ["approved"], 5, ["group-1"]
    )
    sql, params = engine.connection.calls[-1]
    assert "s.embedding <=> CAST" in sql
    assert "v.lifecycle_status = 'approved' AND v.is_current IS TRUE" in sql
    assert "d.project_id IN" in sql
    assert "document_acl" in sql
    assert params["project_ids"] == ["project-1"]
    assert params["principal_ids"] == ["group-1"]
    assert params["minimum_similarity"] == 0.25


def test_document_chunk_expansion_stays_inside_proven_documents() -> None:
    engine = FakeEngine()
    SearchIndex(_postgres_settings(), engine=engine).document_chunks(
        ["doc-1", "doc-1", "doc-2"], ["project-1"], ["approved"], 50
    )
    sql, params = engine.connection.calls[-1]
    assert "d.id IN" in sql
    assert "d.project_id IN" in sql
    assert params["document_ids"] == ["doc-1", "doc-2"]
    assert params["project_ids"] == ["project-1"]
    assert params["top_k"] == 50


def test_exact_identifier_filter_keeps_complete_matching_identifier() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "filename": "KBR-0055-Bronze-Maple-control.md",
                    "heading_path": "Approved Business Decision",
                    "content": "The threshold is CNY 5935.",
                }
            },
            "score": 1.0,
        }
    ]

    assert Retriever._enforce_exact_identifiers("What is KBR-0055?", rows) == rows
    assert Retriever._enforce_exact_identifiers("What is KBR-9999?", rows) == []
    assert Retriever._enforce_exact_identifiers("What is BKR9999?", rows) == []


def test_lexical_signals_extract_business_names_and_acronyms() -> None:
    assert _lexical_signals("沙金枫叶业务的审批阈值是多少？") == ["沙金枫叶"]
    assert _lexical_signals("For Alpine Dolphin, what is the timeout?") == ["Alpine Dolphin"]
    assert _lexical_signals("业务术语 BKR0831 的含义是什么？") == ["BKR0831"]
    assert _lexical_signals("For this, what is the timeout?") == []


def test_exact_identifier_filter_supports_complete_multi_document_comparison() -> None:
    rows = [
        {"hit": {"_source": {"filename": "KBR-0001-a.md"}}, "score": 1.0},
        {"hit": {"_source": {"filename": "KBR-0002-b.md"}}, "score": 0.9},
    ]
    assert Retriever._enforce_exact_identifiers("比较 KBR-0001 与 KBR-0002", rows) == rows
    assert Retriever._enforce_exact_identifiers("比较 KBR-0001 与 KBR-9999", rows) == []


def test_rerank_signal_coverage_keeps_all_exact_comparison_documents() -> None:
    candidates = [
        {
            "hit": {"_source": {"filename": f"KBR-{index:04d}-rule.md", "content": "rule"}},
            "score": 1 / index,
        }
        for index in range(1, 11)
    ]
    # Simulate a model that omitted KBR-0002 from its top results.
    ranked = [(index, 1.0 - index / 10) for index in range(2, 10)]
    selected = Retriever._ensure_signal_coverage(
        "比较 KBR-0001 与 KBR-0002", candidates, ranked, top_k=8
    )
    filenames = [candidates[index]["hit"]["_source"]["filename"] for index, _ in selected]
    assert any("KBR-0001" in filename for filename in filenames)
    assert any("KBR-0002" in filename for filename in filenames)


def test_rerank_passage_includes_governance_metadata() -> None:
    row = {
        "hit": {
            "_source": {
                "filename": "KBR-0001-rule.md",
                "document_status": "approved",
                "version_label": "v1",
                "heading_path": "Approved Decision",
                "content": "CNY 5017",
            }
        }
    }
    passage = Retriever._rerank_passage(row)
    assert "file=KBR-0001-rule.md" in passage
    assert "status=approved" in passage
    assert "heading=Approved Decision" in passage


def test_complete_entity_match_removes_distractor_documents() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "filename": "KBR-0001-Alpine-Dolphin.md",
                    "content": "Alpine Dolphin canonical rule",
                }
            }
        },
        {
            "hit": {
                "_source": {"filename": "KBR-0002-Alpine-Whale.md", "content": "timeout"}
            }
        },
    ]
    result = Retriever._prefer_complete_entity_matches(
        "For Alpine Dolphin, what is the timeout?", rows
    )
    assert result == rows[:1]


def test_content_hash_deduplication_keeps_the_highest_ranked_copy() -> None:
    rows = [
        {
            "hit": {"_source": {"chunk_id": "new", "content_hash": "same"}},
            "score": 1.0,
        },
        {
            "hit": {"_source": {"chunk_id": "old", "content_hash": "same"}},
            "score": 0.5,
        },
    ]
    assert Retriever._deduplicate_content(rows) == rows[:1]
