from collections import UserDict
from types import SimpleNamespace

from app.query_understanding import QueryPlan
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


def test_document_chunk_expansion_stays_inside_proven_documents() -> None:
    client = FakeSearchClient()
    settings = SimpleNamespace(elasticsearch_read_alias="knowledge-chunks-read")
    SearchIndex(settings, client=client).document_chunks(
        ["doc-1", "doc-1", "doc-2"], ["project-1"], ["approved"], 50
    )
    filters = client.search_kwargs["query"]["bool"]["filter"]
    assert {"terms": {"document_id": ["doc-1", "doc-2"]}} in filters
    assert {"terms": {"project_id": ["project-1"]}} in filters
    assert client.search_kwargs["size"] == 50


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
    assert _lexical_signals("雾桥结算引擎 当前受哪个策略管控？") == ["雾桥结算引擎"]
    assert _lexical_signals("岚桥清算发生高金额授权时怎么办？") == ["岚桥清算"]
    assert _lexical_signals("For Mistbridge Clearing（岚桥清算），比较门槛") == [
        "Mistbridge Clearing"
    ]
    assert _lexical_signals("请帮我列出与雪松门相关的具体知识。") == ["雪松门"]
    assert _lexical_signals("雪松门相关的具体知识") == ["雪松门"]
    assert _lexical_signals("雪松门") == ["雪松门"]


def test_lexical_signals_support_natural_knowledge_request_variants() -> None:
    variants = [
        "我想了解一下雪松门有哪些具体知识？",
        "关于“雪松门”，请概括现有资料。",
        "能否介绍一下雪松门吗？",
        "雪松门的资料都说了什么？",
        "雪松门的当前策略是什么？",
        "雪松门中文控制规则是什么？",
        "可以帮我介绍一下雪松门吗？",
        "麻烦你帮我从知识库里查一下雪松门",
        "有关雪松门的信息有哪些？",
        "雪松门，请概括一下。",
    ]
    assert [_lexical_signals(question) for question in variants] == [
        ["雪松门"]
    ] * len(variants)
    assert _lexical_signals("Tell me about Cedar Gate") == ["Cedar Gate"]
    assert _lexical_signals("Knowledge related to Cedar Gate.") == ["Cedar Gate"]
    assert _lexical_signals("What information is relevant to Cedar Gate?") == [
        "Cedar Gate"
    ]


def test_linked_identifier_discovery_uses_only_subject_anchor_rows() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "filename": "requirement.md",
                    "content": "雾桥结算引擎 maps to POL-4101, RTE-6101 and CHG-8101.",
                }
            },
            "score": 1.0,
        },
        {
            "hit": {
                "_source": {
                    "filename": "unrelated.md",
                    "content": "Unrelated POL-4999 must not expand this query.",
                }
            },
            "score": 0.9,
        },
    ]
    assert Retriever._discover_linked_identifiers(
        "雾桥结算引擎 当前受哪个策略管控？", rows
    ) == ["POL-4101", "RTE-6101", "CHG-8101"]


def test_linked_discovery_includes_controlled_cross_language_aliases() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "filename": "97a22abaea7e.txt",
                    "content": (
                        "Chinese business name / 中文业务称谓: 岚桥清算\n"
                        "English operational name / 英文运行称谓: Mistbridge Clearing\n"
                        "control reference / 控制引用: CTL-4601"
                    ),
                }
            },
            "score": 1.0,
        }
    ]
    assert Retriever._discover_linked_identifiers(
        "For Mistbridge Clearing, who owns it?", rows
    ) == ["CTL-4601", "岚桥清算", "Mistbridge Clearing"]


def test_linked_identifier_scope_keeps_downstream_documents() -> None:
    rows = [
        {"hit": {"_source": {"filename": "BIZ-1201.md"}}, "score": 1.0},
        {"hit": {"_source": {"filename": "policy.docx", "content": "POL-4101"}}, "score": 0.9},
        {"hit": {"_source": {"filename": "other.docx", "content": "POL-4999"}}, "score": 0.8},
    ]
    assert Retriever._enforce_exact_identifiers(
        "BIZ-1201 的策略是什么？", rows, ["POL-4101"]
    ) == rows[:2]


def test_entity_scope_keeps_documents_reached_through_linked_identifiers() -> None:
    rows = [
        {"hit": {"_source": {"filename": "requirement.md", "content": "雾桥结算引擎"}}},
        {"hit": {"_source": {"filename": "policy.docx", "content": "POL-4101"}}},
        {"hit": {"_source": {"filename": "unrelated.docx", "content": "POL-4999"}}},
    ]
    assert Retriever._prefer_complete_entity_matches(
        "雾桥结算引擎 当前受哪个策略管控？", rows, ["POL-4101"]
    ) == rows[:2]


def test_natural_request_scopes_to_the_embedded_subject_without_phrase_leakage() -> None:
    rows = [
        {"hit": {"_source": {"filename": "rule.md", "content": "中文控制规则名：雪松门"}}},
        {"hit": {"_source": {"filename": "other.md", "content": "另一个无关控制规则"}}},
    ]
    for question in (
        "请帮我列出与雪松门相关的具体知识。",
        "雪松门相关的具体知识",
        "雪松门",
    ):
        assert Retriever._prefer_complete_entity_matches(question, rows) == rows[:1]
    assert Retriever._prefer_complete_entity_matches(
        "请帮我列出与不存在的松门相关的具体知识。", rows
    ) == []


def test_semantic_query_plan_drives_multiple_retrieval_queries() -> None:
    retriever = Retriever.__new__(Retriever)
    retriever.settings = SimpleNamespace(evidence_top_k=8, rerank_candidates=30)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def retrieve(query, project_ids, statuses, principal_ids=None):
        calls.append((query, tuple(statuses)))
        return []

    retriever._retrieve_for_statuses = retrieve  # type: ignore[method-assign]
    plan = QueryPlan(
        strategy="semantic-qwen-v1",
        language="zh",
        intent="lookup",
        subjects=("翠湖授信",),
        identifiers=(),
        requested_facts=("审批角色", "审批阈值", "失败队列"),
        constraints=("亚太北区", "高金额授权"),
        retrieval_queries=(
            "翠湖授信 亚太北区 高金额授权 审批角色 审批阈值",
            "翠湖授信 高金额授权 失败队列",
        ),
        model_id="plus-planner",
    )
    assert retriever.search("谁点头，卡多少钱，失败后去哪？", ["project-1"], query_plan=plan) == []
    assert [query for query, _ in calls][:3] == [
        "谁点头,卡多少钱,失败后去哪?",
        "翠湖授信 亚太北区 高金额授权 审批角色 审批阈值",
        "翠湖授信 高金额授权 失败队列",
    ]


def test_semantic_anchor_boost_keeps_cross_language_registry_competitive() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "chunk_id": "registry",
                    "content": "English operational name: Greenlake Credit",
                }
            },
            "score": 0.5,
        },
        {
            "hit": {"_source": {"chunk_id": "generic", "content": "APAC North"}},
            "score": 0.6,
        },
    ]
    boosted = Retriever._boost_anchor_matches(
        rows,
        ("Greenlake credit operation", "Greenlake credit"),
    )
    assert boosted[0]["hit"]["_source"]["chunk_id"] == "registry"


def test_semantic_subject_must_be_grounded_before_answering() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "chunk_id": "unrelated-control",
                    "content": "CTL-4616 | APAC North | approval threshold timeout",
                }
            },
            "score": 1.0,
        }
    ]
    assert not Retriever._has_grounded_subject(rows, ("月桂暗门",))
    rows[0]["hit"]["_source"]["content"] += " | 月桂暗门"
    assert Retriever._has_grounded_subject(rows, ("月桂暗门",))


def test_document_diversification_precedes_duplicate_chunks() -> None:
    candidates = [
        {"hit": {"_source": {"document_id": "doc-a", "chunk_id": "a1"}}},
        {"hit": {"_source": {"document_id": "doc-a", "chunk_id": "a2"}}},
        {"hit": {"_source": {"document_id": "doc-b", "chunk_id": "b1"}}},
        {"hit": {"_source": {"document_id": "doc-c", "chunk_id": "c1"}}},
    ]
    result = Retriever._diversify_documents(
        candidates, [(0, 1.0), (1, 0.9), (2, 0.8), (3, 0.7)], top_k=4
    )
    assert [index for index, _ in result] == [0, 2, 3, 1]


def test_candidate_pool_reserves_slot_for_adjacent_document_chunk() -> None:
    fused = [
        {
            "hit": {
                "_source": {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "chunk_ordinal": 0,
                    "content": content,
                }
            },
            "score": score,
        }
        for chunk_id, document_id, content, score in [
            ("a0", "doc-a", "Production contract heading", 1.0),
            ("b0", "doc-b", "Registry mapping", 0.9),
            ("c0", "doc-c", "Control table", 0.8),
            ("d0", "doc-d", "Other useful evidence", 0.7),
        ]
    ]
    related_hits = [
        {
            "_source": {
                "chunk_id": "a1",
                "document_id": "doc-a",
                "chunk_ordinal": 1,
                "content": "POST /platform/v3/evaluate; rejection XR-6801",
            }
        }
    ]

    selected = Retriever._select_rerank_candidate_pool(
        fused,
        related_hits,
        limit=4,
        expansion_slots=1,
        query="正式接口和拒绝码",
    )

    assert [row["hit"]["_source"]["chunk_id"] for row in selected] == [
        "a0",
        "b0",
        "c0",
        "a1",
    ]


def test_short_selected_heading_attaches_adjacent_value_chunk() -> None:
    selected = [
        {
            "hit": {
                "_source": {
                    "chunk_id": "api-0",
                    "document_id": "api-doc",
                    "chunk_ordinal": 0,
                    "content": "Production authorization contract",
                }
            },
            "score": 1.0,
        }
    ]
    related_hits = [
        {
            "_source": {
                "chunk_id": "api-1",
                "document_id": "api-doc",
                "chunk_ordinal": 1,
                "content": "POST /platform/v3/evaluate; rejection XR-6801",
            }
        }
    ]

    expanded = Retriever._attach_short_chunk_neighbors(selected, related_hits)

    assert [row["hit"]["_source"]["chunk_id"] for row in expanded] == [
        "api-0",
        "api-1",
    ]


def test_document_diversification_selects_current_page_within_document() -> None:
    candidates = [
        {
            "hit": {
                "_source": {
                    "document_id": "change",
                    "content": "HISTORICAL REVIEW COPY - NOT THE APPROVAL PAGE",
                }
            }
        },
        {"hit": {"_source": {"document_id": "requirement", "content": "index"}}},
        {
            "hit": {
                "_source": {
                    "document_id": "change",
                    "content": "SIGNED / APPROVED / CURRENT. Effective date: 2026-04-11",
                }
            }
        },
    ]
    result = Retriever._diversify_documents(
        candidates,
        [(0, 1.0), (1, 0.9), (2, 0.8)],
        top_k=3,
        query="正式启用日期是什么？",
    )
    assert [index for index, _ in result] == [2, 1, 0]


def test_current_parameters_are_default_when_question_does_not_say_current() -> None:
    candidates = [
        {"hit": {"_source": {"document_id": "matrix", "content": "历史参数 / RETIRED | old threshold 628800"}}},
        {"hit": {"_source": {"document_id": "matrix", "content": "当前生效参数 / APPROVED CURRENT | threshold 696800"}}},
    ]
    result = Retriever._diversify_documents(candidates, [(0, 1.0), (1, 0.9)], top_k=2, query="金额从多少开始卡住？")
    assert [index for index, _ in result] == [1, 0]


def test_threshold_suffix_does_not_trigger_old_value_intent() -> None:
    candidates = [
        {
            "hit": {
                "_source": {
                    "document_id": "matrix",
                    "content": "HISTORICAL RETIRED | threshold 507700",
                }
            }
        },
        {
            "hit": {
                "_source": {
                    "document_id": "matrix",
                    "content": "APPROVED CURRENT | threshold 575700",
                }
            }
        },
    ]
    result = Retriever._diversify_documents(
        candidates,
        [(0, 1.0), (1, 0.9)],
        top_k=2,
        query="state the current APAC North threshold",
    )
    assert [index for index, _ in result] == [1, 0]


def test_old_as_complete_word_selects_historical_parameters() -> None:
    candidates = [
        {"hit": {"_source": {"document_id": "matrix", "content": "RETIRED historical threshold"}}},
        {"hit": {"_source": {"document_id": "matrix", "content": "APPROVED CURRENT threshold"}}},
    ]
    result = Retriever._diversify_documents(
        candidates,
        [(1, 1.0), (0, 0.9)],
        top_k=2,
        query="what was the old threshold?",
    )
    assert [index for index, _ in result] == [0, 1]


def test_historical_intent_can_still_select_retired_parameters() -> None:
    candidates = [
        {"hit": {"_source": {"document_id": "matrix", "content": "RETIRED 历史参数"}}},
        {"hit": {"_source": {"document_id": "matrix", "content": "APPROVED CURRENT 当前参数"}}},
    ]
    result = Retriever._diversify_documents(candidates, [(1, 1.0), (0, 0.9)], top_k=2, query="历史参数还能用吗？")
    assert [index for index, _ in result] == [0, 1]


def test_document_diversification_avoids_title_only_chunk() -> None:
    candidates = [
        {"hit": {"_source": {"document_id": "policy", "content": "POLICY ARCHITECTURE"}}},
        {
            "hit": {
                "_source": {
                    "document_id": "policy",
                    "content": "Fallback queue | FBQ-5101\nAudit retention | 187 days\n"
                    "Authority | Approved architecture activated by CHG-8101. "
                    "This row contains the operational values used by the answer.",
                }
            }
        },
    ]
    result = Retriever._diversify_documents(
        candidates, [(0, 1.0), (1, 0.9)], top_k=2
    )
    assert [index for index, _ in result] == [1, 0]


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


def test_unknown_near_collision_entity_strictly_abstains() -> None:
    rows = [
        {
            "hit": {
                "_source": {
                    "filename": "9f6c3a012bed.txt",
                    "content": "Mistbridge Clearing maps to CTL-4601.",
                }
            }
        },
        {
            "hit": {
                "_source": {
                    "filename": "15db2ca9be31.txt",
                    "content": "Mistbridge Credit maps to CTL-4602.",
                }
            }
        },
    ]
    assert (
        Retriever._prefer_complete_entity_matches(
            "For Mistbridge Archive, what is the current endpoint?", rows
        )
        == []
    )


def test_generic_subject_does_not_force_strict_entity_abstention() -> None:
    rows = [{"hit": {"_source": {"filename": "policy.md", "content": "current policy"}}}]
    assert Retriever._prefer_complete_entity_matches("系统当前策略是什么？", rows) == rows


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
