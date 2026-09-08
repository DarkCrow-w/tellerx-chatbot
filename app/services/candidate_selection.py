"""主体约束、跨文档关联和相邻证据选择。"""

from __future__ import annotations

from typing import Any

from app.knowledge.evidence import source_status
from app.knowledge.search_text import (
    ACRONYM,
    CONTROLLED_ALIAS,
    EXACT_IDENTIFIER,
    _lexical_signals,
    _query_subject_signals,
    normalize_query,
)
from app.services import candidate_ranking


def enforce_exact_identifiers(
    query: str,
    rows: list[dict[str, Any]],
    linked_identifiers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """强制结果覆盖问题中的全部精确标识符，防止部分命中误答。"""

    identifiers = {
        match.casefold()
        for pattern in (EXACT_IDENTIFIER, ACRONYM)
        for match in pattern.findall(normalize_query(query))
    }
    if not identifiers:
        return rows
    matched: list[dict[str, Any]] = []
    covered: set[str] = set()
    linked = {value.casefold() for value in linked_identifiers or []}
    for row in rows:
        searchable = candidate_ranking.source_text(row)
        row_identifiers = {identifier for identifier in identifiers if identifier in searchable}
        if row_identifiers or any(identifier in searchable for identifier in linked):
            matched.append(row)
        covered.update(row_identifiers)
    return matched if covered == identifiers else []


def discover_linked_identifiers(
    query: str,
    rows: list[dict[str, Any]],
    limit: int = 12,
    anchor_signals: tuple[str, ...] | None = None,
) -> list[str]:
    """从已命中主题锚点中提取受控别名和跨文档引用标识。"""

    query_identifiers = {
        match.casefold()
        for pattern in (EXACT_IDENTIFIER, ACRONYM)
        for match in pattern.findall(normalize_query(query))
    }
    signals = [value.casefold() for value in (anchor_signals or tuple(_lexical_signals(query)))]
    anchors = [
        row
        for row in rows
        if any(signal in candidate_ranking.source_text(row) for signal in signals)
    ]
    if not anchors:
        return []
    discovered: list[str] = []
    for row in anchors[:8]:
        for value in EXACT_IDENTIFIER.findall(candidate_ranking.source_text(row)):
            normalized = value.casefold()
            if normalized not in query_identifiers and normalized not in {
                item.casefold() for item in discovered
            }:
                discovered.append(value.upper())
                if len(discovered) >= limit:
                    return discovered
        source = row.get("hit", {}).get("_source", {})
        alias_text = "\n".join(
            str(source.get(field) or "") for field in ("title_path", "heading_path", "content")
        )
        for value in CONTROLLED_ALIAS.findall(alias_text):
            normalized = value.casefold().strip()
            if (
                normalized
                and normalized not in query_identifiers
                and normalized not in {item.casefold() for item in discovered}
            ):
                discovered.append(value.strip())
                if len(discovered) >= limit:
                    return discovered
    return discovered


def has_grounded_subject(rows: list[dict[str, Any]], subject_signals: tuple[str, ...]) -> bool:
    """判断候选集中是否存在能直接落地问题主题的文本。"""

    signals = [value.casefold() for value in subject_signals if value.strip()]
    return bool(signals) and any(
        signal in candidate_ranking.source_text(row) for row in rows for signal in signals
    )


def select_rerank_candidate_pool(
    fused: list[dict[str, Any]],
    related_hits: list[dict[str, Any]],
    *,
    limit: int,
    expansion_slots: int,
    query: str,
) -> list[dict[str, Any]]:
    """为已证明相关文档的相邻分块预留重排名额。"""

    if limit <= 0:
        return []
    reserve = min(max(0, expansion_slots), max(0, limit - 1))
    base_count = max(1, limit - reserve)
    base = fused[:base_count]
    seen = {str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") for row in base}
    origins: dict[str, list[int]] = {}
    document_order: list[str] = []
    for row in base:
        source = row.get("hit", {}).get("_source", {})
        document_id = str(source.get("document_id") or "")
        if not document_id:
            continue
        if document_id not in origins:
            origins[document_id] = []
            document_order.append(document_id)
        origins[document_id].append(int(source.get("chunk_ordinal") or 0))

    by_document: dict[str, list[dict[str, Any]]] = {}
    for hit in related_hits:
        source = hit.get("_source", {})
        chunk_id = str(source.get("chunk_id") or "")
        document_id = str(source.get("document_id") or "")
        if not chunk_id or chunk_id in seen or document_id not in origins:
            continue
        by_document.setdefault(document_id, []).append(
            {
                "hit": hit,
                "score": 0.0,
                "channels": {"document_expansion"},
                "raw_scores": {},
            }
        )

    expanded: list[dict[str, Any]] = []
    for document_id in document_order:
        options = by_document.get(document_id, [])
        if not options or len(expanded) >= reserve:
            continue
        origin_ordinals = origins[document_id]
        selected = min(
            options,
            key=lambda row: (
                min(
                    abs(int(row["hit"].get("_source", {}).get("chunk_ordinal") or 0) - ordinal)
                    for ordinal in origin_ordinals
                ),
                -candidate_ranking.authority_quality(query, row),
                int(row["hit"].get("_source", {}).get("chunk_ordinal") or 0),
            ),
        )
        expanded.append(selected)
        seen.add(str(selected["hit"].get("_source", {}).get("chunk_id") or ""))

    remaining = [
        row
        for row in fused[base_count:]
        if str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") not in seen
    ]
    return candidate_ranking.deduplicate_content([*base, *expanded, *remaining])[:limit]


def attach_short_chunk_neighbors(
    selected: list[dict[str, Any]],
    related_hits: list[dict[str, Any]],
    *,
    max_extra: int = 4,
    short_threshold: int = 180,
) -> list[dict[str, Any]]:
    """当选中分块过短、疑似只有标题时，补充最近的正文值块。"""

    if max_extra <= 0:
        return selected
    seen = {str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") for row in selected}
    extras: list[dict[str, Any]] = []
    for row in selected:
        if len(extras) >= max_extra:
            break
        source = row.get("hit", {}).get("_source", {})
        if len(str(source.get("content") or "").strip()) >= short_threshold:
            continue
        document_id = str(source.get("document_id") or "")
        origin = int(source.get("chunk_ordinal") or 0)
        options = [
            hit
            for hit in related_hits
            if str(hit.get("_source", {}).get("document_id") or "") == document_id
            and str(hit.get("_source", {}).get("chunk_id") or "") not in seen
        ]
        if not options:
            continue
        hit = min(
            options,
            key=lambda candidate: (
                abs(int(candidate.get("_source", {}).get("chunk_ordinal") or 0) - origin),
                int(candidate.get("_source", {}).get("chunk_ordinal") or 0) < origin,
            ),
        )
        wrapped = {
            "hit": hit,
            "score": float(row.get("score") or 0.0) * 0.99,
            "channels": {"selected_neighbor"},
            "raw_scores": {},
        }
        extras.append(wrapped)
        seen.add(str(hit.get("_source", {}).get("chunk_id") or ""))
    return candidate_ranking.deduplicate_content([*selected, *extras])


def attach_provenance_bridge_chunks(
    query: str,
    selected: list[dict[str, Any]],
    related_hits: list[dict[str, Any]],
    linked_identifiers: list[str],
    *,
    max_extra: int = 2,
) -> list[dict[str, Any]]:
    """在下游事实旁保留“主题→引用标识”的来源桥接分块。

    重排器天然偏好含最终值的段落，跨文档查询时可能因此丢掉证明二者关系的
    需求或注册表段落。这里仅保留已召回的确定性映射，不推断或创造新关系。
    """

    if max_extra <= 0 or not selected or not related_hits:
        return selected
    query_ids = {
        value.casefold()
        for pattern in (EXACT_IDENTIFIER, ACRONYM)
        for value in pattern.findall(normalize_query(query))
    }
    subject_signals = query_ids or {
        value.casefold()
        for value in (*_query_subject_signals(query), *_lexical_signals(query))
        if len(value.strip()) >= 2
    }
    downstream_ids = {
        value.casefold()
        for row in selected
        for value in EXACT_IDENTIFIER.findall(candidate_ranking.source_text(row))
        if value.casefold() not in query_ids
    }
    downstream_ids.update(value.casefold() for value in linked_identifiers)
    if not subject_signals or not downstream_ids:
        return selected

    seen = {str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") for row in selected}
    preferred_types = {
        "business-requirement",
        "requirement",
        "terminology-registry",
        "mapping",
        "reference-index",
    }
    options: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    for position, hit in enumerate(related_hits):
        source = hit.get("_source", {})
        chunk_id = str(source.get("chunk_id") or "")
        if not chunk_id or chunk_id in seen or source_status(source) != "approved":
            continue
        wrapped = {"hit": hit}
        text = candidate_ranking.source_text(wrapped)
        matched_subjects = sum(signal in text for signal in subject_signals)
        matched_links = sum(identifier in text for identifier in downstream_ids)
        if not matched_subjects or not matched_links:
            continue
        document_type = str(source.get("document_type") or "").casefold()
        options.append(
            (
                (
                    int(document_type in preferred_types),
                    matched_subjects,
                    matched_links,
                    -position,
                ),
                {
                    "hit": hit,
                    "score": 0.0,
                    "channels": {"provenance_bridge"},
                    "raw_scores": {},
                },
            )
        )
    if not options:
        return selected
    extras = [row for _, row in sorted(options, key=lambda item: item[0], reverse=True)]
    return candidate_ranking.deduplicate_content([*selected, *extras[:max_extra]])


def prefer_complete_entity_matches(
    query: str,
    rows: list[dict[str, Any]],
    linked_identifiers: list[str] | None = None,
    *,
    subject_signals: tuple[str, ...] | None = None,
    subject_document_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Ground subject filters in retrieved data, retaining document inheritance and bridges."""
    signals = (
        subject_signals if subject_signals is not None else tuple(_query_subject_signals(query))
    )
    signals = tuple(
        s.casefold()
        for s in signals
        if s not in {"业务", "系统", "项目", "模块", "规则", "接口", "文档", "策略"}
    )
    if not signals:
        return rows
    # An ungrounded rule fragment must never veto already-recalled facts.
    if not subject_document_ids and not all(
        any(signal in candidate_ranking.source_text(row) for row in rows) for signal in signals
    ):
        return rows
    documents = set(subject_document_ids)
    documents.update(
        str(row["hit"]["_source"].get("document_id") or "")
        for row in rows
        if any(signal in candidate_ranking.source_text(row) for signal in signals)
    )
    linked = {value.casefold() for value in linked_identifiers or []}
    return [
        row
        for row in rows
        if str(row["hit"]["_source"].get("document_id") or "") in documents - {""}
        or any(signal in candidate_ranking.source_text(row) for signal in signals)
        or any(identifier in candidate_ranking.source_text(row) for identifier in linked)
    ]
