"""Prompt construction and citation validation for evidence-bound answers.

This module is deliberately independent from database persistence and model
routing.  Keeping the contract pure makes the highest-risk safety rule—the
model may only return claims backed by exact source quotes—easy to audit and
unit test.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.contracts.schemas import CitationOut, ClaimOut
from app.integrations.search import EXACT_IDENTIFIER
from app.knowledge.chunking import estimate_tokens
from app.knowledge.evidence import Evidence

SYSTEM_PROMPT = """You are an evidence-bound enterprise knowledge assistant.
Use only the EVIDENCE provided by the user. Never use web knowledge, memory, or unstated assumptions.
Respond in the same language as the question while retaining exact professional acronyms.
Every factual claim must cite one or more evidence IDs and include an exact, contiguous quote copied from that evidence.
Return at most 6 claims. Prefer the most important supported facts and keep every quote to the shortest exact span that proves its claim.
Answer every field requested by the question and preserve exact API paths, error codes, identifiers, dates, roles, and numeric values.
Treat governance owners, business responsible persons, approval roles, operators, and escalation contacts as different facts. Never substitute one role for another merely because both are people or roles.
When evidence supplies bilingual names or roles, include both exact language forms. If a requested field names a language, include the exact localized value from the evidence even when the rest of the answer uses another language.
For a cross-document join, when the downstream evidence does not name the question's business subject, cite both the bridge evidence that maps the subject to the downstream identifier and the downstream evidence that supplies the value.
If evidence is insufficient, return insufficient_evidence. If authoritative sources conflict, return conflict and explain both sides without choosing one.
Return exactly one JSON object with this shape:
{
  "status": "answered|insufficient_evidence|conflict",
  "answer": "concise answer",
  "claims": [
    {"text": "one factual claim", "evidence": [{"id": "chunk-id", "quote": "exact source quote"}]}
  ]
}
Do not wrap JSON in Markdown. Do not cite evidence that does not directly support the claim.
"""


@dataclass(slots=True)
class ValidatedAnswer:
    """声明和来源原文均已通过服务端校验的回答。"""

    status: str
    answer: str
    claims: list[ClaimOut]
    sources: list[CitationOut]


class AnswerValidationError(ValueError):
    """模型输出违反证据响应契约时抛出。"""


def _normalize_for_quote(text: str) -> str:
    """只规范化排版空白，不放宽原文引用的精确匹配要求。"""

    return re.sub(r"\s+", " ", text).strip()


def _repair_ellipsis_quote(quote: str, source: str) -> str | None:
    """仅在有界范围内把模型省略号恢复成连续的来源原文。"""

    parts = [
        _normalize_for_quote(part)
        for part in re.split(r"(?:\.{3,}|…+)", quote)
        if _normalize_for_quote(part)
    ]
    if len(parts) < 2 or any(len(part) < 2 for part in parts):
        return None
    normalized_source = _normalize_for_quote(source)
    start = normalized_source.find(parts[0])
    if start < 0:
        return None
    cursor = start + len(parts[0])
    for part in parts[1:]:
        position = normalized_source.find(part, cursor)
        if position < 0:
            return None
        cursor = position + len(part)
    if cursor - start > 800:
        return None
    return normalized_source[start:cursor]


def _repair_formatting_quote(quote: str, source: str) -> str | None:
    """当差异仅来自排版标点时，恢复对应的精确来源片段。"""

    def compact(text: str) -> tuple[str, list[int]]:
        """保留字母数字及其原始位置，供紧凑文本反向定位。"""

        characters: list[str] = []
        positions: list[int] = []
        for position, character in enumerate(text):
            if unicodedata.category(character)[0] in {"L", "N"}:
                characters.append(character.lower())
                positions.append(position)
        return "".join(characters), positions

    compact_quote, _ = compact(quote)
    compact_source, source_positions = compact(source)
    if len(compact_quote) < 4:
        return None
    start = compact_source.find(compact_quote)
    if start < 0:
        return None
    repaired = source[
        source_positions[start] : source_positions[start + len(compact_quote) - 1] + 1
    ].strip()
    return repaired if repaired and len(repaired) <= 800 else None


def _repair_anchored_quote(quote: str, source: str) -> str | None:
    """围绕未变化的名称、标识和值，尝试恢复精确来源文本。"""

    folded_source = source.casefold()
    precision = [
        *EXACT_IDENTIFIER.findall(quote),
        *re.findall(r"(?<![A-Za-z0-9])\d+(?:[,.]\d+)*", quote),
    ]
    normalized_source = re.sub(r"(?<=\d),(?=\d)", "", folded_source)
    for value in precision:
        normalized = re.sub(r"(?<=\d),(?=\d)", "", value.casefold())
        if normalized not in normalized_source:
            return None
    descriptive = [
        *re.findall(
            r"\b[A-Z][A-Za-z0-9-]{2,}(?:[ \t]+[A-Z][A-Za-z0-9-]{2,})+",
            quote,
        ),
        *re.findall(r"[\u3400-\u9fff]{2,20}", quote),
    ]
    descriptive = list(dict.fromkeys(descriptive))
    matched_descriptive = [value for value in descriptive if value.casefold() in folded_source]
    if descriptive and len(matched_descriptive) / len(descriptive) < 0.7:
        return None
    anchors = list(dict.fromkeys([*matched_descriptive, *precision]))
    located: list[tuple[int, int]] = []
    for anchor in anchors:
        candidate = anchor.casefold()
        position = folded_source.find(candidate)
        if position < 0 and re.fullmatch(r"\d+(?:[,.]\d+)*", anchor):
            candidate = re.sub(r"(?<=\d),(?=\d)", "", candidate)
            position = folded_source.find(candidate)
        if position >= 0:
            located.append((position, position + len(candidate)))
    if not located:
        return None
    start = min(position for position, _ in located)
    end = max(position for _, position in located)
    if len(located) == 1:
        start = source.rfind("\n", 0, start) + 1
        line_end = source.find("\n", end)
        end = len(source) if line_end < 0 else line_end
    repaired = source[start:end].strip()
    return repaired if repaired and len(repaired) <= 800 else None


def refusal_text(
    question: str,
    *,
    validation_failed: bool = False,
    generation_unavailable: bool = False,
) -> str:
    """返回确定性拒答文案，同时不泄露模型供应商内部诊断信息。"""

    chinese = bool(re.search(r"[\u3400-\u9fff]", question))
    if generation_unavailable:
        return (
            "已找到相关资料，但生成模型当前不可用，因此本次不生成结论。"
            if chinese
            else "Relevant sources were found, but the generation model is currently unavailable; no conclusion was returned."
        )
    if validation_failed:
        return (
            "已找到相关资料，但模型输出未通过引用校验，因此本次不生成结论。"
            if chinese
            else "Relevant sources were found, but the model output failed citation validation; no conclusion was returned."
        )
    return (
        "知识库中没有找到足以支持结论的证据。"
        if chinese
        else "The knowledge base does not contain sufficient evidence for a conclusion."
    )


def build_evidence_prompt(
    question: str,
    evidence: list[Evidence],
    prompt_version: str,
    requested_facts: tuple[str, ...] = (),
) -> str:
    """只把已选证据和稳定来源信息序列化进模型提示词。"""

    blocks = []
    for item in evidence:
        location = ", ".join(
            part
            for part in [
                item.heading_path,
                f"page={item.page_number}" if item.page_number else None,
                f"sheet={item.sheet_name}" if item.sheet_name else None,
                f"cells={item.cell_range}" if item.cell_range else None,
            ]
            if part
        )
        blocks.append(
            "\n".join(
                [
                    f"[EVIDENCE id={item.chunk_id}]",
                    f"file={item.filename}",
                    f"status={item.document_status}",
                    f"version={item.version_label or 'unspecified'}",
                    f"location={location or 'unspecified'}",
                    item.content,
                    "[/EVIDENCE]",
                ]
            )
        )
    requested_fact_block = "\n".join(f"- {fact}" for fact in requested_facts)
    return (
        f"PROMPT_VERSION: {prompt_version}\n"
        f"QUESTION:\n{question}\n\n"
        f"REQUESTED_FACTS:\n{requested_fact_block or '- derive directly from QUESTION'}\n\n"
        f"EVIDENCE:\n" + "\n\n".join(blocks)
    )


def fit_evidence_budget(evidence: list[Evidence], max_tokens: int) -> list[Evidence]:
    """在上下文预算内按原顺序选择证据，避免重排语义被破坏。"""

    selected: list[Evidence] = []
    used = 0
    for item in evidence:
        # 固定开销用于估算每段内容外围的来源标签和结构标记。
        cost = estimate_tokens(item.content) + 40
        if selected and used + cost > max_tokens:
            continue
        selected.append(item)
        used += cost
    return selected


def _answer_fields(payload: dict[str, Any]) -> tuple[str, str, list[Any]]:
    """读取并校验模型响应的三个顶层字段。"""

    status = payload.get("status")
    answer = payload.get("answer")
    claims = payload.get("claims")
    if (
        status not in {"answered", "insufficient_evidence", "conflict"}
        or not isinstance(answer, str)
        or not isinstance(claims, list)
    ):
        raise AnswerValidationError("Missing or invalid status, answer, or claims")
    return status, answer, claims


def _verified_quote(quote: str, evidence: Evidence) -> str:
    """返回来源中连续存在的引用，必要时执行有边界的排版修复。"""

    if _normalize_for_quote(quote) in _normalize_for_quote(evidence.content):
        return quote
    repaired = _repair_ellipsis_quote(quote, evidence.content)
    repaired = repaired or _repair_formatting_quote(quote, evidence.content)
    repaired = repaired or _repair_anchored_quote(quote, evidence.content)
    if repaired is None:
        raise AnswerValidationError(f"Quote is not present in source {evidence.chunk_id}")
    return repaired


def _validated_citation(
    raw_citation: Any,
    evidence_by_id: dict[str, Evidence],
) -> tuple[str, str, CitationOut]:
    """校验一条引用并转换为公开来源 DTO。"""

    if not isinstance(raw_citation, dict):
        raise AnswerValidationError("Invalid citation")
    chunk_id = raw_citation.get("id")
    quote = raw_citation.get("quote")
    if (
        not isinstance(chunk_id, str)
        or chunk_id not in evidence_by_id
        or not isinstance(quote, str)
        or len(quote.strip()) < 2
    ):
        raise AnswerValidationError("Citation ID or quote is invalid")
    item = evidence_by_id[chunk_id]
    quote = _verified_quote(quote, item)
    source = CitationOut(
        chunk_id=chunk_id,
        document_id=item.document_id,
        filename=item.filename,
        document_status=item.document_status,
        heading_path=item.heading_path,
        page_number=item.page_number,
        sheet_name=item.sheet_name,
        cell_range=item.cell_range,
        quote=quote.strip(),
    )
    return chunk_id, quote, source


def _validated_claim(
    raw_claim: Any,
    evidence_by_id: dict[str, Evidence],
    source_map: dict[tuple[str, str], CitationOut],
) -> ClaimOut:
    """校验一项事实声明以及它的全部引用。"""

    if not isinstance(raw_claim, dict) or not isinstance(raw_claim.get("text"), str):
        raise AnswerValidationError("Invalid claim")
    claim_text = raw_claim["text"].strip()
    if not claim_text:
        raise AnswerValidationError("Claim text cannot be empty")
    raw_citations = raw_claim.get("evidence")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise AnswerValidationError("Every claim requires evidence")

    citation_ids: list[str] = []
    for raw_citation in raw_citations:
        chunk_id, quote, source = _validated_citation(raw_citation, evidence_by_id)
        citation_ids.append(chunk_id)
        source_map[(chunk_id, quote)] = source
    return ClaimOut(text=claim_text, citations=list(dict.fromkeys(citation_ids)))


def validate_answer(payload: dict[str, Any], evidence: list[Evidence]) -> ValidatedAnswer:
    """校验状态、声明、证据 ID 和逐字来源引用。

    顶层自由文本不会直接返回；最终答案由已校验声明重新拼装，确保未引用的句子
    无法绕过证据门禁。
    """

    status, answer, raw_claims = _answer_fields(payload)
    if status == "insufficient_evidence":
        if raw_claims:
            raise AnswerValidationError("Insufficient-evidence responses cannot contain claims")
        return ValidatedAnswer(status=status, answer=answer, claims=[], sources=[])
    if not raw_claims:
        raise AnswerValidationError("Answered and conflict responses must contain claims")

    by_id = {item.chunk_id: item for item in evidence}
    source_map: dict[tuple[str, str], CitationOut] = {}
    claims = [_validated_claim(raw_claim, by_id, source_map) for raw_claim in raw_claims]
    if status == "conflict":
        conflict_ids = {citation for claim in claims for citation in claim.citations}
        if len(conflict_ids) < 2:
            raise AnswerValidationError("Conflict responses require at least two evidence sources")

    return ValidatedAnswer(
        status=status,
        answer="\n".join(claim.text for claim in claims),
        claims=claims,
        sources=list(source_map.values()),
    )
