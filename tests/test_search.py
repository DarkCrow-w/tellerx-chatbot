from collections import UserDict
from types import SimpleNamespace

from app.search import Retriever, SearchIndex, _lexical_signals


class FakeIndices:
    def __init__(self):
        self.actions = None

    def exists(self, *, index):
        return True

    def get_alias(self, *, name):
        return {}

    def get_mapping(self, *, index):
        return {index: {"mappings": {"properties": {"identifiers": {"type": "keyword"}}}}}

    def update_aliases(self, *, actions):
        self.actions = actions


class FakeSearchClient:
    def __init__(self):
        self.indices = SimpleNamespace(exists=lambda *, index: True)
        self.searched_index = None
        self.search_kwargs = None

    def search(self, *, index, **kwargs):
        self.searched_index = index
        self.search_kwargs = kwargs
        return {"hits": {"hits": []}}


def test_missing_alias_response_does_not_remove_status_as_index() -> None:
    indices = FakeIndices()
    client = SimpleNamespace(indices=indices)
    settings = SimpleNamespace(
        search_index_name="knowledge-chunks-model-1024-v1",
        elasticsearch_read_alias="knowledge-chunks-read",
        elasticsearch_write_alias="knowledge-chunks-write",
        qwen_embedding_dimensions=1024,
    )
    SearchIndex(settings, client=client).ensure_index()
    assert indices.actions == [
        {
            "add": {
                "index": "knowledge-chunks-model-1024-v1",
                "alias": "knowledge-chunks-read",
            }
        },
        {
            "add": {
                "index": "knowledge-chunks-model-1024-v1",
                "alias": "knowledge-chunks-write",
                "is_write_index": True,
            }
        },
    ]


def test_alias_response_accepts_mapping_wrapper_from_elasticsearch_client() -> None:
    client = SimpleNamespace(
        indices=SimpleNamespace(
            get_alias=lambda *, name: UserDict(
                {"physical-index": {"aliases": UserDict({name: {}})}}
            )
        )
    )
    settings = SimpleNamespace()
    assert SearchIndex(settings, client=client)._alias_indexes("read-alias") == [
        "physical-index"
    ]


def test_alias_response_accepts_object_api_response_body() -> None:
    response = SimpleNamespace(
        body={"physical-index": {"aliases": {"read-alias": {}}}}
    )
    client = SimpleNamespace(
        indices=SimpleNamespace(get_alias=lambda *, name: response)
    )
    assert SearchIndex(SimpleNamespace(), client=client)._alias_indexes(
        "read-alias"
    ) == ["physical-index"]


def test_vector_search_uses_read_alias_and_pre_filters() -> None:
    client = FakeSearchClient()
    settings = SimpleNamespace(
        search_index_name="knowledge-chunks-qwen3.7-text-embedding-1024-v1",
        elasticsearch_read_alias="knowledge-chunks-read",
        vector_num_candidates=400,
        vector_min_similarity=0.25,
    )

    SearchIndex(settings, client=client).vector_search(
        [0.1], ["project-1"], ["approved"], 5, ["group-1"]
    )

    assert client.searched_index == settings.elasticsearch_read_alias
    assert client.search_kwargs["knn"]["num_candidates"] == 400
    assert client.search_kwargs["knn"]["similarity"] == 0.25
    filters = client.search_kwargs["knn"]["filter"]["bool"]["filter"]
    assert {"terms": {"project_id": ["project-1"]}} in filters
    assert any("bool" in item for item in filters)


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
