"""从查询计划构建业务主体与事实查询。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.knowledge.search_text import _query_subject_signals, lexical_tokens, normalize_query

if TYPE_CHECKING:
    from app.services.query_understanding import QueryPlan


def subject_signals(query: str, query_plan: QueryPlan | None) -> tuple[str, ...]:
    """主体必须直接出现在用户问题中，避免使用模型自行补充的名称。"""
    values = query_plan.subjects if query_plan else tuple(_query_subject_signals(query))
    normalized = normalize_query(query).casefold()
    return tuple(dict.fromkeys(s for s in values if len(s) >= 2 and s.casefold() in normalized))


def fact_queries(query: str, query_plan: QueryPlan | None, subjects: tuple[str, ...]) -> list[str]:
    constraints = " ".join(query_plan.constraints) if query_plan else ""
    facts = list(query_plan.requested_facts) if query_plan else []
    stripped = query
    for subject in subjects:
        stripped = re.sub(re.escape(subject), " ", stripped, flags=re.IGNORECASE)
    queries = [normalize_query(f"{fact} {constraints}") for fact in facts[:3]]
    queries.append(normalize_query(stripped))
    return list(dict.fromkeys(q for q in queries if len(q) > 1))[:4]


def grounded_subject_prefix(
    subject: str, rows: list[dict[str, Any]], query_plan: QueryPlan | None
) -> str | None:
    """仅在文件名提供依据时，去掉被误接在业务名称后的动作描述。"""
    if not query_plan:
        return None
    context = " ".join(
        (*query_plan.scenario_terms, *query_plan.requested_facts, *query_plan.constraints)
    )
    candidates = []
    for row in rows:
        filename = str(row["hit"]["_source"].get("filename") or "")
        size = 0
        for left, right in zip(subject, filename):
            if left.casefold() != right.casefold():
                break
            size += 1
        prefix, suffix = subject[:size], subject[size:]
        # Restrict this repair to Chinese names with a scenario-bearing suffix.
        # Similar names differing by region, digits or English letters must not collapse.
        if size >= 4 and suffix and re.fullmatch(r"[\u3400-\u9fff]+", prefix):
            terms = [t for t in lexical_tokens(suffix) if len(t) >= 2]
            operational_suffix = re.fullmatch(
                r"(?:同步|异步|生产|测试|线上|线下)(?:处理|环境)?", suffix
            )
            if operational_suffix or any(term in context for term in terms):
                candidates.append(prefix)
    return max(candidates, key=len) if candidates else None


def fact_coverage(row: dict[str, Any], fact: str) -> float:
    source = row["hit"]["_source"]
    text = normalize_query(
        str(source.get("title_path") or "") + " " + str(source.get("content") or "")
    ).casefold()
    normalized = normalize_query(fact).casefold()
    if normalized in text:
        return 1.0
    terms = [term for term in lexical_tokens(fact) if len(term) >= 2]
    return sum(term in text for term in terms) / len(terms) if terms else 0.0
