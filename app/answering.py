from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chunking import estimate_tokens
from app.config import Settings
from app.model_router import NoModelAvailable, QwenModelRouter, route_tier
from app.models import Chunk, Conversation, Document, DocumentVersion, Message, QueryTrace
from app.query_understanding import (
    QueryPlan,
    QueryUnderstandingService,
    fallback_query_plan,
)
from app.qwen import parse_json_object
from app.schemas import ChatResponse, CitationOut, ClaimOut
from app.search import (
    EXACT_IDENTIFIER,
    Evidence,
    Retriever,
    _lexical_signals,
    normalize_query,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an evidence-bound enterprise knowledge assistant.
Use only the EVIDENCE provided by the user. Never use web knowledge, memory, or unstated assumptions.
Respond in the same language as the question while retaining exact professional acronyms.
Every factual claim must cite one or more evidence IDs and include an exact, contiguous quote copied from that evidence.
Return at most 6 claims. Prefer the most important supported facts and keep every quote to the shortest exact span that proves its claim.
Answer every field requested by the question and preserve exact API paths, error codes, identifiers, dates, roles, and numeric values.
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
    status: str
    answer: str
    claims: list[ClaimOut]
    sources: list[CitationOut]


class AnswerValidationError(ValueError):
    pass


BRIDGE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ZH_BRIDGE_SUBJECT = re.compile(
    r"^([\u3400-\u9fff]{2,20}?)\s*(?:当前|的|在|受|使用|由|如果|若|最新)"
)


def _normalize_for_quote(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _repair_ellipsis_quote(quote: str, source: str) -> str | None:
    """Expand model ellipses only into a bounded, exact contiguous source span."""
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
    """Recover an exact source span when only layout punctuation differs.

    Models sometimes turn a table row such as ``A | B`` into ``A: B`` even
    when instructed to copy it.  The returned value is still an exact,
    contiguous slice of the source; letters and numbers may not be changed,
    inserted, or removed.
    """

    def compact(text: str) -> tuple[str, list[int]]:
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
    if not repaired or len(repaired) > 800:
        return None
    return repaired


def _repair_anchored_quote(quote: str, source: str) -> str | None:
    """Recover exact source text around unchanged names, IDs, and values."""
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
        *re.findall(r"\b[A-Z][A-Za-z0-9-]{2,}(?:\s+[A-Z][A-Za-z0-9-]{2,})+", quote),
        *re.findall(r"[\u3400-\u9fff]{2,20}", quote),
    ]
    descriptive = list(dict.fromkeys(descriptive))
    matched_descriptive = [
        value for value in descriptive if value.casefold() in folded_source
    ]
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
    if not repaired or len(repaired) > 800:
        return None
    return repaired


def _refusal_text(question: str, *, validation_failed: bool = False) -> str:
    chinese = bool(re.search(r"[\u3400-\u9fff]", question))
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
    selected: list[Evidence] = []
    used = 0
    for item in evidence:
        cost = estimate_tokens(item.content) + 40
        if selected and used + cost > max_tokens:
            continue
        selected.append(item)
        used += cost
    return selected


def validate_answer(payload: dict, evidence: list[Evidence]) -> ValidatedAnswer:
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
                repaired = _repair_ellipsis_quote(quote, item.content)
                if repaired is None:
                    repaired = _repair_formatting_quote(quote, item.content)
                if repaired is None:
                    repaired = _repair_anchored_quote(quote, item.content)
                if repaired is None:
                    raise AnswerValidationError(f"Quote is not present in source {chunk_id}")
                quote = repaired
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
    # The model's free-form top-level answer is not independently citable. Build
    # the returned conclusion only from the individually validated claim list so
    # an uncited sentence cannot bypass the citation gate.
    validated_answer = "\n".join(claim.text for claim in claims)
    return ValidatedAnswer(
        status=status,
        answer=validated_answer,
        claims=claims,
        sources=list(source_map.values()),
    )


def attach_cross_document_bridges(
    question: str, validated: ValidatedAnswer, evidence: list[Evidence]
) -> ValidatedAnswer:
    """Add deterministic provenance bridges without adding or changing claims."""
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
        if any(
            any(anchor in item.content.casefold() for anchor in subject_anchors)
            for item in cited
        ):
            continue
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
        bridge = next(
            (
                item
                for item in evidence
                if item.chunk_id not in claim.citations
                and any(
                    anchor in item.filename.casefold()
                    or anchor in item.content.casefold()
                    for anchor in subject_anchors
                )
                and any(
                    identifier
                    in " ".join([item.filename, item.heading_path or "", item.content]).casefold()
                    for identifier in downstream_ids
                )
            ),
            None,
        )
        if bridge is None:
            continue
        quote_parts = [
            part.strip()
            for part in re.split(r"(?<=[。！？.!?])\s*|\n+", bridge.content)
            if part.strip()
        ]
        quote = next(
            (
                part
                for part in quote_parts
                if any(identifier in part.casefold() for identifier in downstream_ids)
            ),
            quote_parts[0] if quote_parts else bridge.content.strip(),
        )
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
                    page_number=bridge.page_number,
                    sheet_name=bridge.sheet_name,
                    cell_range=bridge.cell_range,
                    quote=quote,
                )
            )
            source_keys.add(key)
    return validated


class AnswerService:
    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        router: QwenModelRouter,
        query_understanding: QueryUnderstandingService | None = None,
    ):
        self.settings = settings
        self.retriever = retriever
        self.router = router
        self.query_understanding = query_understanding

    @staticmethod
    def _get_conversation(db: Session, conversation_id: str | None) -> Conversation:
        conversation = db.get(Conversation, conversation_id) if conversation_id else None
        if not conversation:
            conversation = Conversation()
            db.add(conversation)
            db.flush()
        return conversation

    def _persist(
        self,
        db: Session,
        *,
        conversation: Conversation,
        question: str,
        validated: ValidatedAnswer,
        model_id: str | None,
        trace_id: str,
        started_at: float,
        project_ids: list[str],
        evidence: list[Evidence],
        query_plan: QueryPlan,
    ) -> Message:
        retrieval_index = getattr(
            self.settings,
            "elasticsearch_read_alias",
            "knowledge-chunks-read",
        )
        search_backend = getattr(self.retriever, "index", None)
        if search_backend and hasattr(search_backend, "trace_index_name"):
            retrieval_index = search_backend.trace_index_name()
        db.add(Message(conversation_id=conversation.id, role="user", content=question, trace_id=trace_id))
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=validated.answer,
            answer_status=validated.status,
            model_id=model_id,
            trace_id=trace_id,
            citations=[source.model_dump() for source in validated.sources],
        )
        db.add(message)
        db.add(
            QueryTrace(
                trace_id=trace_id,
                normalized_query=normalize_query(question),
                project_ids=project_ids,
                index_name=retrieval_index,
                retrieval_json={
                    "prompt_version": getattr(self.settings, "prompt_version", None),
                    "query_understanding": query_plan.as_trace_dict(),
                    "embedding_fingerprint": getattr(
                        self.settings, "embedding_fingerprint", None
                    ),
                    "evidence": [
                        {
                            "chunk_id": item.chunk_id,
                            "document_id": item.document_id,
                            "version_id": item.version_id,
                            "score": item.score,
                        }
                        for item in evidence
                    ]
                },
                answer_status=validated.status,
                model_id=model_id,
                latency_ms=(time.perf_counter() - started_at) * 1000,
            )
        )
        db.commit()
        return message

    def _validate_live_sources(
        self, db: Session, validated: ValidatedAnswer, evidence: list[Evidence]
    ) -> None:
        if not getattr(self.settings, "validate_citations_against_database", True):
            return
        cited = {citation for claim in validated.claims for citation in claim.citations}
        if not cited:
            return
        rows = db.execute(
            select(
                Chunk.id,
                DocumentVersion.lifecycle_status,
                DocumentVersion.technical_status,
                DocumentVersion.is_current,
                Document.is_deleted,
            )
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(Chunk.id.in_(cited))
        ).all()
        live = {
            chunk_id
            for chunk_id, lifecycle, technical, is_current, is_deleted in rows
            if not is_deleted
            and technical == "searchable"
            and (lifecycle == "draft" or (lifecycle == "approved" and is_current))
        }
        if live != cited:
            raise AnswerValidationError("A cited source is no longer searchable")

    def answer(
        self,
        db: Session,
        *,
        question: str,
        project_ids: list[str],
        conversation_id: str | None,
        pinned_model: str | None,
    ) -> ChatResponse:
        started_at = time.perf_counter()
        trace_id = str(uuid.uuid4())
        conversation = self._get_conversation(db, conversation_id)
        if self.query_understanding is not None:
            query_plan = self.query_understanding.understand(
                db,
                question,
                pinned_model=pinned_model,
            )
            evidence = self.retriever.search(
                question,
                project_ids,
                query_plan=query_plan,
            )
        else:
            query_plan = fallback_query_plan(question, "service-not-configured")
            evidence = self.retriever.search(question, project_ids)
        if not evidence:
            validated = ValidatedAnswer(
                status="insufficient_evidence",
                answer=_refusal_text(question),
                claims=[],
                sources=[],
            )
            message = self._persist(
                db,
                conversation=conversation,
                question=question,
                validated=validated,
                model_id=None,
                trace_id=trace_id,
                started_at=started_at,
                project_ids=project_ids,
                evidence=evidence,
                query_plan=query_plan,
            )
            return ChatResponse(
                status=validated.status,
                answer=validated.answer,
                claims=validated.claims,
                sources=validated.sources,
                model_id=None,
                route_tier=None,
                conversation_id=conversation.id,
                message_id=message.id,
                trace_id=trace_id,
            )

        version_pairs: dict[str, set[str]] = {}
        for item in evidence:
            version_pairs.setdefault(item.document_id, set()).add(item.version_id)
        has_conflict = any(len(versions) > 1 for versions in version_pairs.values())
        tier = route_tier(question, [item.document_id for item in evidence[:6]], has_conflict)
        evidence = fit_evidence_budget(evidence, 4000 if tier == "plus" else 7000)
        user_prompt = build_evidence_prompt(question, evidence, self.settings.prompt_version)

        model_id: str | None = None
        attempted_tier = tier
        for attempt in range(2):
            if attempt == 1 and tier == "plus" and not pinned_model:
                attempted_tier = "max"
            try:
                call = self.router.call(
                    db,
                    tier=attempted_tier,
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    pinned_model=pinned_model,
                    prompt_version=self.settings.prompt_version,
                )
                model_id = call.model_id
                validated = validate_answer(parse_json_object(call.content), evidence)
                validated = attach_cross_document_bridges(question, validated, evidence)
                if validated.status == "insufficient_evidence":
                    validated.answer = _refusal_text(question)
                self._validate_live_sources(db, validated, evidence)
                message = self._persist(
                    db,
                    conversation=conversation,
                    question=question,
                    validated=validated,
                    model_id=model_id,
                    trace_id=trace_id,
                    started_at=started_at,
                    project_ids=project_ids,
                    evidence=evidence,
                    query_plan=query_plan,
                )
                return ChatResponse(
                    status=validated.status,
                    answer=validated.answer,
                    claims=validated.claims,
                    sources=validated.sources,
                    model_id=model_id,
                    route_tier=attempted_tier,
                    conversation_id=conversation.id,
                    message_id=message.id,
                    trace_id=trace_id,
                )
            except (TypeError, ValueError, json.JSONDecodeError, NoModelAvailable) as exc:
                if isinstance(exc, NoModelAvailable):
                    break
                logger.warning(
                    "Answer output rejected; retrying with citation correction (%s: %s)",
                    type(exc).__name__,
                    str(exc),
                )
                user_prompt += (
                    "\n\nPREVIOUS_OUTPUT_REJECTED: Return a fresh JSON object. "
                    "Copy each quote exactly and contiguously from one evidence block. "
                    "Do not paraphrase inside quote fields. Return at most 6 claims, use "
                    "the shortest sufficient quote for each claim, and keep the JSON compact. "
                    "Prioritize the most important supported facts instead of producing an "
                    "exhaustive answer. "
                    "For cross-document joins, cite the subject-to-identifier bridge evidence "
                    "together with the downstream value evidence."
                )
                if pinned_model:
                    continue
        validated = ValidatedAnswer(
            status="insufficient_evidence",
            answer=_refusal_text(question, validation_failed=True),
            claims=[],
            sources=[],
        )
        message = self._persist(
            db,
            conversation=conversation,
            question=question,
            validated=validated,
            model_id=model_id,
            trace_id=trace_id,
            started_at=started_at,
            project_ids=project_ids,
            evidence=evidence,
            query_plan=query_plan,
        )
        return ChatResponse(
            status=validated.status,
            answer=validated.answer,
            claims=[],
            sources=[],
            model_id=model_id,
            route_tier=attempted_tier,
            conversation_id=conversation.id,
            message_id=message.id,
            trace_id=trace_id,
        )
