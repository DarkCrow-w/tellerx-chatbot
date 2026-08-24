"""Application service that orchestrates retrieval, generation, and persistence."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.schemas import ChatResponse
from app.core.config import Settings
from app.db.models import Chunk, Conversation, Document, DocumentVersion, Message, QueryTrace
from app.integrations.qwen import ChatCallResult, parse_json_object
from app.integrations.search import normalize_query
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

logger = logging.getLogger(__name__)


class EvidenceRetriever(Protocol):
    """Minimal retrieval port required by the answer use case."""

    def search(self, query: str, project_ids: list[str]) -> list[Evidence]: ...


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
    ) -> ChatCallResult: ...


class AnswerService:
    def __init__(
        self,
        settings: Settings,
        retriever: EvidenceRetriever,
        router: AnswerModelRouter,
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
        requested_tier: str | None,
        actual_tier: str | None,
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
                    "routing": {
                        "requested_tier": requested_tier,
                        "actual_tier": actual_tier,
                    },
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
