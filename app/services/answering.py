"""Application service that orchestrates retrieval, generation, and persistence."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.schemas import ChatResponse, CitationOut
from app.core.config import Settings
from app.db.models import Chunk, Conversation, Document, DocumentVersion, Message, QueryTrace
from app.integrations.qwen import ChatCallResult, parse_json_object
from app.integrations.search import _lexical_signals, normalize_query
from app.knowledge.evidence import Evidence
from app.services.answer_contract import (
    SYSTEM_PROMPT,
    AnswerValidationError,
    ValidatedAnswer,
    build_evidence_prompt,
    fit_evidence_budget,
    refusal_text,
    validate_answer,
)
from app.services.model_router import NoModelAvailable, route_tier
from app.services.query_understanding import (
    QueryPlan,
    QueryUnderstandingService,
    fallback_query_plan,
)

logger = logging.getLogger(__name__)


class EvidenceRetriever(Protocol):
    """Minimal retrieval port required by the answer use case."""

    def search(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None = None,
        query_plan: QueryPlan | None = None,
    ) -> list[Evidence]: ...


class AnswerModelRouter(Protocol):
    """Model routing port; concrete quota and failover policy lives elsewhere."""

    def call(
        self,
        db: Session,
        *,
        tier: str,
        system_prompt: str,
        user_prompt: str,
        pinned_model: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatCallResult: ...

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
        bridge_candidates = [
            item
            for item in evidence
            if item.chunk_id not in claim.citations
            and any(
                anchor
                in " ".join(
                    [item.filename, item.heading_path or "", item.content]
                ).casefold()
                for anchor in subject_anchors
            )
            and any(
                identifier
                in " ".join([item.filename, item.heading_path or "", item.content]).casefold()
                for identifier in downstream_ids
            )
        ]
        preferred_bridge_types = {
            "business-requirement",
            "requirement",
            "terminology-registry",
            "mapping",
            "reference-index",
        }
        bridge = max(
            bridge_candidates,
            key=lambda item: (
                item.document_type.casefold() in preferred_bridge_types,
                sum(
                    anchor in " ".join([item.heading_path or "", item.content]).casefold()
                    for anchor in subject_anchors
                ),
                sum(
                    identifier
                    in " ".join([item.heading_path or "", item.content]).casefold()
                    for identifier in downstream_ids
                ),
            ),
            default=None,
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
        retriever: EvidenceRetriever,
        router: AnswerModelRouter,
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
        requested_tier: str | None,
        actual_tier: str | None,
        query_plan: QueryPlan,
    ) -> Message:
        retrieval_index = getattr(
            self.settings,
            "search_index_name",
            "postgresql:chunk_search_index",
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
                    "routing": {
                        "requested_tier": requested_tier,
                        "actual_tier": actual_tier,
                    },
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
                answer=refusal_text(question),
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
                requested_tier=None,
                actual_tier=None,
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
        user_prompt = build_evidence_prompt(
            question,
            evidence,
            self.settings.prompt_version,
            query_plan.requested_facts,
        )

        model_id: str | None = None
        attempted_tier = tier
        failure_kind = "validation"
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
                    validated.answer = refusal_text(question)
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
                    requested_tier=tier,
                    actual_tier=attempted_tier,
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
                    failure_kind = "provider"
                    # Complex questions normally use Max. When every Max model
                    # is unavailable, one evidence-bound Plus attempt is safer
                    # and more useful than failing the request outright. Pinned
                    # evaluation runs never degrade, preserving reproducibility.
                    if tier == "max" and attempt == 0 and not pinned_model:
                        logger.warning(
                            "Max tier unavailable; degrading one grounded answer attempt to Plus"
                        )
                        attempted_tier = "plus"
                        continue
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
            answer=refusal_text(
                question,
                validation_failed=failure_kind == "validation",
                generation_unavailable=failure_kind == "provider",
            ),
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
            requested_tier=tier,
            actual_tier=attempted_tier,
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
