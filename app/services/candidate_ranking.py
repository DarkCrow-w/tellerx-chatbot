"""候选融合、分数调整和证据转换。"""

from __future__ import annotations

import re
from typing import Any

from app.knowledge.evidence import Evidence, source_status
from app.knowledge.search_text import _lexical_signals, normalize_query


def rrf(*ranked_lists: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
    """用倒数排名融合多个召回通道，避免不同分值尺度直接相加。"""

    combined: dict[str, dict[str, Any]] = {}
    for channel, ranked in enumerate(ranked_lists):
        for rank, hit in enumerate(ranked, start=1):
            chunk_id = hit.get("_source", {}).get("chunk_id") or hit.get("_id")
            if not chunk_id:
                continue
            row = combined.setdefault(
                chunk_id,
                {"hit": hit, "score": 0.0, "channels": set(), "raw_scores": {}},
            )
            row["score"] += 1.0 / (k + rank)
            row["channels"].add(channel)
            row["raw_scores"][channel] = float(hit.get("_score") or 0.0)
    for row in combined.values():
        source = row["hit"].get("_source", {})
        if source_status(source) == "approved":
            row["score"] *= 1.08
    return sorted(combined.values(), key=lambda row: row["score"], reverse=True)


def merge_fused(
    primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并原问题与语义规划通道，并略微降低扩展通道权重。"""

    merged: dict[str, dict[str, Any]] = {}
    for channel, rows in enumerate((primary, secondary)):
        weight = 1.0 if channel == 0 else 0.92
        for row in rows:
            source = row.get("hit", {}).get("_source", {})
            chunk_id = str(source.get("chunk_id") or row.get("hit", {}).get("_id") or "")
            if not chunk_id:
                continue
            if chunk_id not in merged:
                merged[chunk_id] = {
                    **row,
                    "score": float(row.get("score") or 0.0) * weight,
                }
            else:
                merged[chunk_id]["score"] += float(row.get("score") or 0.0) * weight
    return sorted(merged.values(), key=lambda row: row["score"], reverse=True)


def merge_evidence_channels(
    baseline: list[Evidence], planned: list[Evidence], *, limit: int
) -> list[Evidence]:
    """保留原问题召回顺序，同时允许语义规划补充不重复的证据。"""

    scores: dict[str, float] = {}
    items: dict[str, Evidence] = {}
    for channel in (baseline, planned):
        for rank, item in enumerate(channel, 1):
            items[item.chunk_id] = item
            scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1.0 / (60 + rank)
    return [items[key] for key in sorted(scores, key=lambda key: -scores[key])[:limit]]


def boost_anchor_matches(
    rows: list[dict[str, Any]], anchor_signals: tuple[str, ...]
) -> list[dict[str, Any]]:
    """提升命中主题锚点的候选，降低语义相似但主题错误的概率。"""

    signals = [value.casefold() for value in anchor_signals if value.strip()]
    if not signals:
        return rows
    boosted = []
    for row in rows:
        score = float(row.get("score") or 0.0)
        if any(signal in source_text(row) for signal in signals):
            score *= 1.35
        boosted.append({**row, "score": score})
    return sorted(boosted, key=lambda row: row["score"], reverse=True)


def deduplicate_content(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按内容哈希去重，避免相同段落挤占证据预算。"""

    unique = []
    seen: set[str] = set()
    for row in rows:
        source = row.get("hit", {}).get("_source", {})
        key = str(source.get("content_hash") or source.get("chunk_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(row)
    return unique


def authority_quality(query: str, row: dict[str, Any]) -> int:
    """按问题意图评价时效权威性，默认优先正式当前值而非历史描述。"""

    normalized_query = normalize_query(query).casefold()
    text = source_text(row)
    authoritative = (
        "approved",
        "current",
        "effective date",
        "signed",
        "authoritative",
        "正式",
        "当前",
        "生效",
        "已批准",
    )
    historical = (
        "not the approval page",
        "not current",
        "not the final decision",
        "retired",
        "historical",
        "superseded",
        "deprecated",
        "draft",
        "candidate",
        "never approved",
        "旧值",
        "退役",
        "历史",
        "草稿",
        "候选",
        "作废",
    )
    asks_for_history = any(
        term in normalized_query for term in ("历史", "旧值", "过去", "退役")
    ) or bool(re.search(r"\b(?:historical|old|retired)\b", normalized_query))
    if asks_for_history:
        return 2 * sum(term in text for term in historical) - 2 * sum(
            term in text for term in authoritative
        )
    return 2 * sum(term in text for term in authoritative) - 3 * sum(
        term in text for term in historical
    )


def diversify_documents(
    candidates: list[dict[str, Any]],
    ranked: list[tuple[int, float]],
    top_k: int,
    query: str = "",
) -> list[tuple[int, float]]:
    """先为每份文档保留一个权威代表，再追加同文档的其他候选。"""

    valid = [(index, score) for index, score in ranked if 0 <= index < len(candidates)]
    seen_indexes: set[int] = set()
    document_order: list[str] = []
    by_document: dict[str, list[tuple[int, tuple[int, float]]]] = {}
    deduplicated: list[tuple[int, float]] = []
    for position, item in enumerate(valid):
        index = item[0]
        if index in seen_indexes:
            continue
        seen_indexes.add(index)
        deduplicated.append(item)
        source = candidates[index]["hit"].get("_source", {})
        document_id = str(source.get("document_id") or source.get("filename") or index)
        if document_id not in by_document:
            document_order.append(document_id)
            by_document[document_id] = []
        by_document[document_id].append((position, item))

    def representative_priority(positioned: tuple[int, tuple[int, float]]) -> tuple[int, bool, int]:
        position, (candidate_index, _) = positioned
        candidate = candidates[candidate_index]
        content = str(candidate["hit"].get("_source", {}).get("content") or "")
        return authority_quality(query, candidate), len(content.strip()) >= 100, -position

    representatives = []
    for document_id in document_order:
        _, representative = max(by_document[document_id], key=representative_priority)
        representatives.append(representative)
    representative_indexes = {item[0] for item in representatives}
    deferred = [item for item in deduplicated if item[0] not in representative_indexes]
    return [*representatives, *deferred][:top_k]


def to_evidence(source: dict[str, Any], score: float) -> Evidence:
    """把搜索后端记录转换成业务层稳定的证据对象。"""

    return Evidence(
        chunk_id=source["chunk_id"],
        document_id=source["document_id"],
        version_id=source["version_id"],
        project_id=source["project_id"],
        filename=source["filename"],
        document_status=source_status(source),
        document_type=source["document_type"],
        content=source["content"],
        heading_path=source.get("title_path") or source.get("heading_path"),
        section_id=source.get("section_id"),
        section_level=source.get("section_level"),
        breadcrumb=tuple(
            part.strip()
            for part in str(source.get("section_path") or source.get("title_path") or "").split(">")
            if part.strip()
        ),
        location_confidence=(1.0 if int(source.get("section_level") or 0) > 0 else 0.7),
        page_number=source.get("page_number"),
        sheet_name=source.get("sheet_name"),
        cell_range=source.get("cell_range"),
        version_label=source.get("version_label"),
        score=score,
    )


def source_text(row: dict[str, Any]) -> str:
    """拼接可用于规则判断的来源文本，并统一为大小写不敏感形式。"""

    source = row["hit"].get("_source", {})
    return " ".join(
        str(source.get(field) or "")
        for field in ("filename", "title_path", "heading_path", "content")
    ).casefold()


def rerank_passage(row: dict[str, Any]) -> str:
    """为重排模型序列化正文及必要的来源、版本和状态上下文。"""

    source = row["hit"]["_source"]
    return "\n".join(
        [
            f"file={source.get('filename', '')}",
            f"status={source_status(source)}",
            f"version={source.get('version_label', '')}",
            f"heading={source.get('title_path') or source.get('heading_path', '')}",
            str(source.get("content") or ""),
        ]
    )


def boost_section_matches(
    rows: list[dict[str, Any]], section_hints: tuple[str, ...]
) -> list[dict[str, Any]]:
    """把用户明确提及的标题片段作为软加权，不把它变成硬过滤。"""

    hints = [normalize_query(value).casefold() for value in section_hints if value.strip()]
    boosted = []
    for row in rows:
        source = row.get("hit", {}).get("_source", {})
        path = str(source.get("section_path") or source.get("title_path") or "").casefold()
        matches = sum(hint in path for hint in hints)
        boosted.append({**row, "score": float(row.get("score") or 0.0) * (1 + 0.2 * matches)})
    return sorted(boosted, key=lambda item: item["score"], reverse=True)


def ensure_signal_coverage(
    query: str,
    candidates: list[dict[str, Any]],
    ranked: list[tuple[int, float]],
    top_k: int,
    additional_signals: tuple[str, ...] = (),
) -> list[tuple[int, float]]:
    """在重排结果中补回关键字信号覆盖，避免模型漏掉精确条件。"""

    selected = [(index, score) for index, score in ranked if 0 <= index < len(candidates)]
    selected = list(dict.fromkeys(selected))
    mandatory_indexes: list[int] = []
    signals = tuple(dict.fromkeys((*_lexical_signals(query), *additional_signals)))
    for signal in (value.casefold() for value in signals):
        if any(signal in source_text(candidates[index]) for index in mandatory_indexes):
            continue
        mandatory = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if signal in source_text(candidate)
            ),
            None,
        )
        if mandatory is not None:
            mandatory_indexes.append(mandatory)
    mandatory_rows = [(index, candidates[index]["score"]) for index in mandatory_indexes[:top_k]]
    remaining = []
    seen = set(mandatory_indexes)
    for item in selected:
        if item[0] not in seen:
            seen.add(item[0])
            remaining.append(item)
    return [*mandatory_rows, *remaining[: max(0, top_k - len(mandatory_rows))]]
