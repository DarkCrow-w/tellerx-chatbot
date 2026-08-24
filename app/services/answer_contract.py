"""Prompt construction and citation validation for evidence-bound answers.

This module is deliberately independent from database persistence and model
routing.  Keeping the contract pure makes the highest-risk safety rule—the
model may only return claims backed by exact source quotes—easy to audit and
unit test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.contracts.schemas import CitationOut, ClaimOut
from app.knowledge.chunking import estimate_tokens
from app.knowledge.evidence import Evidence

SYSTEM_PROMPT = """You are an evidence-bound enterprise knowledge assistant.
Use only the EVIDENCE provided by the user. Never use web knowledge, memory, or unstated assumptions.
Respond in the same language as the question while retaining exact professional acronyms.
Every factual claim must cite one or more evidence IDs and include an exact, contiguous quote copied from that evidence.
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
    """An answer whose claims and source quotes passed server-side checks."""

    status: str
    answer: str
    claims: list[ClaimOut]
    sources: list[CitationOut]


class AnswerValidationError(ValueError):
    """Raised when model output violates the evidence response contract."""


def _normalize_for_quote(text: str) -> str:
    """Normalize layout whitespace without weakening exact quote matching."""

    return re.sub(r"\s+", " ", text).strip()


def refusal_text(
    question: str,
    *,
    validation_failed: bool = False,
    generation_unavailable: bool = False,
) -> str:
    """Return a deterministic refusal without leaking provider diagnostics."""

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


def build_evidence_prompt(question: str, evidence: list[Evidence], prompt_version: str) -> str:
    """Serialize only selected evidence and stable provenance into the model prompt."""

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
    return (
        f"PROMPT_VERSION: {prompt_version}\n"
        f"QUESTION:\n{question}\n\n"
        f"EVIDENCE:\n" + "\n\n".join(blocks)
    )


def fit_evidence_budget(evidence: list[Evidence], max_tokens: int) -> list[Evidence]:
    """Keep evidence order while fitting the configured context budget."""

    selected: list[Evidence] = []
    used = 0
    for item in evidence:
        # The fixed overhead covers provenance labels around every content block.
        cost = estimate_tokens(item.content) + 40
        if selected and used + cost > max_tokens:
            continue
        selected.append(item)
        used += cost
    return selected


def validate_answer(payload: dict[str, Any], evidence: list[Evidence]) -> ValidatedAnswer:
    """Validate status, claims, evidence IDs, and verbatim source quotes.

    The free-form top-level model answer is never returned directly.  The final
    answer is reconstructed from validated claim text so an uncited sentence
    cannot bypass the citation gate.
    """

    allowed_statuses = {"answered", "insufficient_evidence", "conflict"}
    status = payload.get("status")
    answer = payload.get("answer")
    raw_claims = payload.get("claims")
    if status not in allowed_statuses or not isinstance(answer, str) or not isinstance(raw_claims, list):
        raise AnswerValidationError("Missing or invalid status, answer, or claims")
    if status == "insufficient_evidence":
        if raw_claims:
            raise AnswerValidationError("Insufficient-evidence responses cannot contain claims")
        return ValidatedAnswer(status=status, answer=answer, claims=[], sources=[])
    if not raw_claims:
        raise AnswerValidationError("Answered and conflict responses must contain claims")

    by_id = {item.chunk_id: item for item in evidence}
    claims: list[ClaimOut] = []
    source_map: dict[tuple[str, str], CitationOut] = {}
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, dict) or not isinstance(raw_claim.get("text"), str):
            raise AnswerValidationError("Invalid claim")
        claim_text = raw_claim["text"].strip()
        if not claim_text:
            raise AnswerValidationError("Claim text cannot be empty")
        raw_citations = raw_claim.get("evidence")
        if not isinstance(raw_citations, list) or not raw_citations:
            raise AnswerValidationError("Every claim requires evidence")
        citation_ids = []
        for raw_citation in raw_citations:
            if not isinstance(raw_citation, dict):
                raise AnswerValidationError("Invalid citation")
            chunk_id = raw_citation.get("id")
            quote = raw_citation.get("quote")
            if chunk_id not in by_id or not isinstance(quote, str) or len(quote.strip()) < 2:
                raise AnswerValidationError("Citation ID or quote is invalid")
            item = by_id[chunk_id]
            if _normalize_for_quote(quote) not in _normalize_for_quote(item.content):
                raise AnswerValidationError(f"Quote is not present in source {chunk_id}")
            citation_ids.append(chunk_id)
            source_map[(chunk_id, quote)] = CitationOut(
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
        claims.append(ClaimOut(text=claim_text, citations=list(dict.fromkeys(citation_ids))))
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
