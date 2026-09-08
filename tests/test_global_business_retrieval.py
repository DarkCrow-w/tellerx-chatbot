from app.services import business_queries, candidate_ranking, candidate_selection

"""Regression coverage for business identity, scope inheritance and evidence budgets."""

from dataclasses import replace
from unittest.mock import Mock

from app.core.config import Settings
from app.knowledge.evidence import Evidence
from app.knowledge.search_text import _query_subject_signals
from app.services.query_understanding import fallback_query_plan
from app.services.retrieval import Retriever


def row(key, document, filename, content):
    return {
        "score": 1.0,
        "hit": {
            "_source": {
                "chunk_id": key,
                "document_id": document,
                "filename": filename,
                "content": content,
            }
        },
    }


def test_possessive_business_name_does_not_include_requested_facts():
    assert _query_subject_signals("青禾清算的签名算法和密钥轮换周期是什么？") == ["青禾清算"]
    assert _query_subject_signals("FalconPay的重试次数是多少？") == ["FalconPay"]


def test_weak_ungrounded_rule_cannot_delete_initial_recall():
    rows = [row("answer", "a", "长风容量.docx", "生产环境并发上限为600。")]
    assert (
        candidate_selection.prefer_complete_entity_matches(
            "长风容量生产环境与测试环境的并发上限各是多少？", rows
        )
        == rows
    )


def test_verified_subject_inherits_document_and_rejects_other_business():
    rows = [
        row("intro", "a", "overview.docx", "本规范适用于青禾清算。"),
        row("answer", "a", "overview.docx", "密钥轮换周期为33天。"),
        row("wrong", "b", "澄海支付.docx", "密钥轮换周期为32天。"),
    ]
    actual = candidate_selection.prefer_complete_entity_matches(
        "原始口语问题", rows, subject_signals=("青禾清算",)
    )
    assert [r["hit"]["_source"]["chunk_id"] for r in actual] == ["intro", "answer"]


def test_multi_business_comparison_keeps_both_verified_documents():
    rows = [
        row("a", "a", "青禾清算.docx", "重试4次"),
        row("b", "b", "澄海支付.docx", "重试3次"),
        row("c", "c", "其他.docx", "重试9次"),
    ]
    actual = candidate_selection.prefer_complete_entity_matches(
        "比较两个业务", rows, subject_signals=("青禾清算", "澄海支付")
    )
    assert len(actual) == 2


def test_invented_model_subject_is_not_a_scope_constraint():
    query = "签名失败怎么办？"
    plan = replace(fallback_query_plan(query, "test"), subjects=("不存在的业务",))
    assert business_queries.subject_signals(query, plan) == ()


def test_business_group_channel_preserves_acl_and_multiple_documents():
    index = Mock()
    index.business_documents.return_value = ["architecture", "operations"]
    retriever = Retriever(Settings(), index, Mock())
    retriever._retrieve_for_statuses = Mock(return_value=[])
    query = "青禾清算的生产告警规则是什么？"
    plan = replace(
        fallback_query_plan(query, "test"),
        subjects=("青禾清算",),
        requested_facts=("告警阈值", "值班角色"),
        constraints=("生产",),
    )
    _, subjects, docs = retriever._business_channel(query, ["project"], ["reader"], plan, [], None)
    assert subjects == ("青禾清算",)
    assert docs == ["architecture", "operations"]
    index.business_documents.assert_called_once_with(
        "青禾清算", ["project"], ["approved"], ["reader"], limit=10
    )
    for call in retriever._retrieve_for_statuses.call_args_list:
        assert call.args[1:] == (["project"], ["approved"], ["reader"], docs)
    assert retriever._retrieve_for_statuses.call_args_list[0].args[0] == "告警阈值 生产"


def test_explicit_file_channel_never_discovers_other_business_documents():
    index = Mock()
    retriever = Retriever(Settings(), index, Mock())
    retriever._retrieve_for_statuses = Mock(return_value=[])
    query = "接口规范文档的超时是多少？"
    plan = replace(fallback_query_plan(query, "test"), requested_facts=("超时",))
    _, _, docs = retriever._business_channel(
        query, ["project"], ["reader"], plan, [], ["chosen-file"]
    )
    index.business_documents.assert_not_called()
    assert docs == ["chosen-file"]
    assert all(
        c.args[-1] == ["chosen-file"] for c in retriever._retrieve_for_statuses.call_args_list
    )


def evidence(key):
    return Evidence(key, "doc", "version", "project", "file", "approved", "spec", "content")


def test_planned_fact_is_not_starved_by_full_baseline_budget():
    baseline = [evidence(str(i)) for i in range(12)]
    planned = [evidence("new-fact")]
    result = candidate_ranking.merge_evidence_channels(baseline, planned, limit=8)
    assert "new-fact" in {r.chunk_id for r in result}
    assert len(result) == 8


def test_each_requested_fact_gets_a_candidate_without_duplicate_chunks():
    retriever = Retriever(Settings(), Mock(), Mock())
    plan = replace(
        fallback_query_plan("青禾清算的超时和审批角色是什么？", "test"),
        requested_facts=("超时", "审批角色"),
    )
    rows = [
        row("noise", "a", "青禾清算.docx", "运行参考和一般说明"),
        row("timeout", "a", "青禾清算.docx", "超时400毫秒"),
        row("owner", "a", "青禾清算.docx", "审批角色为亚太审批岗"),
    ]
    ranked = retriever._prioritize_facts(rows, plan)
    assert [r["hit"]["_source"]["chunk_id"] for r in ranked[:2]] == ["timeout", "owner"]
    assert len(ranked) == 3


def test_updated_search_uses_one_subject_decision_after_original_recall():
    retriever = Retriever(Settings(), Mock(), Mock())
    plan = fallback_query_plan("青禾清算的密钥多久更换？", "test")
    retriever._search_once = Mock(return_value=[])
    retriever.search("青禾清算的密钥多久更换？", [], query_plan=plan)
    retriever._search_once.assert_called_once_with("青禾清算的密钥多久更换？", [], None, plan)


def test_action_appended_to_subject_is_repaired_only_with_document_evidence():
    plan = replace(
        fallback_query_plan("碧湖计费同步处理超时后怎么办？", "test"),
        subjects=("碧湖计费同步处理",),
        scenario_terms=("超时处理",),
        requested_facts=("目标队列",),
    )
    rows = [row("a", "a", "碧湖计费二期_架构设计.docx", "碧湖计费业务规范。")]
    assert business_queries.grounded_subject_prefix("碧湖计费同步处理", rows, plan) == "碧湖计费"
    assert business_queries.grounded_subject_prefix("碧湖计费香港", rows, plan) is None
    assert business_queries.grounded_subject_prefix("碧湖计费2", rows, plan) is None
    assert business_queries.grounded_subject_prefix("不存在同步处理", rows, plan) is None


def test_repaired_subject_is_verified_again_before_filtering():
    index = Mock()
    index.business_documents.side_effect = [[], ["a"]]
    retriever = Retriever(Settings(), index, Mock())
    retriever._retrieve_for_statuses = Mock(return_value=[])
    query = "碧湖计费同步处理超时后怎么办？"
    plan = replace(
        fallback_query_plan(query, "test"),
        subjects=("碧湖计费同步处理",),
        scenario_terms=("超时处理",),
    )
    _, subjects, docs = retriever._business_channel(
        query, [], None, plan, [row("a", "a", "碧湖计费二期.docx", "正文")], None
    )
    assert subjects == ("碧湖计费",)
    assert docs == ["a"]
    assert index.business_documents.call_args_list[1].args[0] == "碧湖计费"


def test_sync_qualifier_does_not_become_part_of_the_business_identity():
    plan = replace(
        fallback_query_plan("玄鹭通知同步处理超时怎么办？", "test"),
        subjects=("玄鹭通知同步",),
        scenario_terms=("处理超时",),
    )
    rows = [row("a", "a", "玄鹭通知二期.docx", "玄鹭通知业务规范")]
    assert business_queries.grounded_subject_prefix("玄鹭通知同步", rows, plan) == "玄鹭通知"
    assert business_queries.grounded_subject_prefix("玄鹭通知香港", rows, plan) is None
