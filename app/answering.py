from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chunking import estimate_tokens
from app.config import Settings
from app.model_router import NoModelAvailable, QwenModelRouter, route_tier
from app.models import Chunk, Conversation, Document, DocumentVersion, Message, QueryTrace
from app.qwen import parse_json_object
from app.schemas import ChatResponse, CitationOut, ClaimOut
from app.search import Evidence, Retriever, normalize_query

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
    status: str
    answer: str
    claims: list[ClaimOut]
    sources: list[CitationOut]


class AnswerValidationError(ValueError):
    pass


def _normalize_for_quote(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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


class AnswerService:
    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        router: QwenModelRouter,
    ):
        self.settings = settings
        self.retriever = retriever
        self.router = router

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
