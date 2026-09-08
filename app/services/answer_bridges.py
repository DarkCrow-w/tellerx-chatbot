"""为已经通过原文校验的答案补齐跨文档引用。"""

from __future__ import annotations

import re

from app.contracts.schemas import CitationOut
from app.knowledge.evidence import Evidence
from app.knowledge.search_text import _lexical_signals
from app.services.answer_contract import ValidatedAnswer

BRIDGE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ZH_BRIDGE_SUBJECT = re.compile(
    r"^([\u3400-\u9fff]{2,20}?)\s*(?:当前|的|在|受|使用|由|如果|若|最新)"
)


def attach_cross_document_bridges(
    question: str, validated: ValidatedAnswer, evidence: list[Evidence]
) -> ValidatedAnswer:
    """为跨文档声明补充确定性的来源桥接，但不新增或改写事实。"""

    if validated.status not in {"answered", "conflict"}:
        return validated
    query_ids = {value.casefold() for value in BRIDGE_IDENTIFIER.findall(question)}
    subject_anchors = {
        *query_ids,
        *(value.casefold() for value in _lexical_signals(question)),
        *(match.group(1).casefold() for match in ZH_BRIDGE_SUBJECT.finditer(question)),
    }
    if not subject_anchors:
        return validated
    by_id = {item.chunk_id: item for item in evidence}
    source_keys = {(source.chunk_id, source.quote) for source in validated.sources}
    for claim in validated.claims:
        cited = [by_id[citation] for citation in claim.citations if citation in by_id]
        downstream_ids = {
            value.casefold()
            for item in cited
            for value in BRIDGE_IDENTIFIER.findall(
                " ".join([item.heading_path or "", item.content])
            )
            if value.casefold() not in query_ids
        }
        if not downstream_ids:
            continue
        bridge = _select_bridge(evidence, claim.citations, subject_anchors, downstream_ids)
        if bridge is None:
            continue
        quote = _bridge_quote(bridge.content, downstream_ids)
        if not quote:
            continue
        claim.citations = list(dict.fromkeys([*claim.citations, bridge.chunk_id]))
        key = (bridge.chunk_id, quote)
        if key not in source_keys:
            validated.sources.append(
                CitationOut(
                    chunk_id=bridge.chunk_id,
                    document_id=bridge.document_id,
                    filename=bridge.filename,
                    document_status=bridge.document_status,
                    heading_path=bridge.heading_path,
                    section_id=bridge.section_id,
                    breadcrumb=list(bridge.breadcrumb),
                    section_level=bridge.section_level,
                    location_confidence=bridge.location_confidence,
                    page_number=bridge.page_number,
                    sheet_name=bridge.sheet_name,
                    cell_range=bridge.cell_range,
                    quote=quote,
                )
            )
            source_keys.add(key)
    return validated


def _select_bridge(
    evidence: list[Evidence],
    cited_ids: list[str],
    subject_anchors: set[str],
    downstream_ids: set[str],
) -> Evidence | None:
    """优先选择同时连接业务主体和下游编号的正式映射文档。"""
    preferred_types = {
        "business-requirement",
        "requirement",
        "terminology-registry",
        "mapping",
        "reference-index",
    }
    candidates = []
    for item in evidence:
        if item.chunk_id in cited_ids:
            continue
        text = " ".join([item.filename, item.heading_path or "", item.content]).casefold()
        matches_subject = any(anchor in text for anchor in subject_anchors)
        matches_reference = any(identifier in text for identifier in downstream_ids)
        if matches_subject and matches_reference:
            candidates.append(item)

    def priority(item: Evidence) -> tuple[bool, int, int]:
        text = " ".join([item.heading_path or "", item.content]).casefold()
        return (
            item.document_type.casefold() in preferred_types,
            sum(anchor in text for anchor in subject_anchors),
            sum(identifier in text for identifier in downstream_ids),
        )

    return max(candidates, key=priority, default=None)


def _bridge_quote(content: str, downstream_ids: set[str]) -> str:
    """截取包含下游编号的原文句子；没有命中时沿用首句。"""
    sentences = [
        part.strip() for part in re.split(r"(?<=[。！？.!?])\s*|\n+", content) if part.strip()
    ]
    for sentence in sentences:
        if any(identifier in sentence.casefold() for identifier in downstream_ids):
            return sentence
    return sentences[0] if sentences else content.strip()
