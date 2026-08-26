"""Application service that orchestrates retrieval, generation, and persistence."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.contracts.schemas import ChatResponse, CitationOut
from app.core.config import Settings
from app.db.models import Conversation, Message
from app.integrations.qwen import ChatCallResult, parse_json_object
from app.integrations.search import _lexical_signals, normalize_query
from app.knowledge.evidence import Evidence
from app.repositories.chat import ChatRepository
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
    """回答用例所需的最小证据检索接口。"""

    def search(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None = None,
        query_plan: QueryPlan | None = None,
    ) -> list[Evidence]:
        """在指定项目和数据权限范围内返回排序后的证据。"""

        ...


class AnswerModelRouter(Protocol):
    """回答服务依赖的模型路由接口；配额和故障转移策略由实现层负责。"""

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
    ) -> ChatCallResult:
        """按指定层级调用回答模型并返回标准化结果。"""

        ...


@dataclass(slots=True)
class AnswerPreparation:
    """一次问答在调用生成模型前准备好的全部上下文。"""

    query_plan: QueryPlan
    evidence: list[Evidence]
    requested_tier: str | None
    user_prompt: str | None


@dataclass(slots=True)
class GenerationResult:
    """生成尝试的结果；失败时 ``validated`` 为空并记录失败类别。"""

    validated: ValidatedAnswer | None
    model_id: str | None
    actual_tier: str | None
    failure_kind: str = "validation"


BRIDGE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ZH_BRIDGE_SUBJECT = re.compile(
    r"^([\u3400-\u9fff]{2,20}?)\s*(?:当前|的|在|受|使用|由|如果|若|最新)"
)

CITATION_CORRECTION_PROMPT = """

PREVIOUS_OUTPUT_REJECTED: Return a fresh JSON object. Copy each quote exactly and
contiguously from one evidence block. Do not paraphrase inside quote fields. Return
at most 6 claims, use the shortest sufficient quote for each claim, and keep the JSON
compact. Prioritize the most important supported facts instead of producing an
exhaustive answer. For cross-document joins, cite the subject-to-identifier bridge
evidence together with the downstream value evidence.
"""


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
        bridge_candidates = [
            item
            for item in evidence
            if item.chunk_id not in claim.citations
            and any(
                anchor
                in " ".join([item.filename, item.heading_path or "", item.content]).casefold()
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
                    identifier in " ".join([item.heading_path or "", item.content]).casefold()
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
    """编排查询理解、证据检索、受约束生成、校验和会话持久化。"""

    def __init__(
        self,
        settings: Settings,
        retriever: EvidenceRetriever,
        router: AnswerModelRouter,
        query_understanding: QueryUnderstandingService | None = None,
        repository: ChatRepository | None = None,
    ):
        """注入回答链路所需的策略配置和端口实现。"""

        self.settings = settings
        self.retriever = retriever
        self.router = router
        self.query_understanding = query_understanding
        # 可选参数保持独立单元测试易用；生产组合根会显式注入 Repository。
        self.repository = repository or ChatRepository()

    @staticmethod
    def _get_conversation(db: Session, conversation_id: str | None) -> Conversation:
        """返回已有会话，或为无会话请求创建并立即取得主键。"""

        # 保留静态方法供既有测试和调用方使用；新代码通过实例 Repository 调用。
        return ChatRepository().get_or_create_conversation(db, conversation_id)

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
        """在同一事务中保存问答消息和完整查询追踪信息。"""

        retrieval_index = getattr(
            self.settings,
            "search_index_name",
            "postgresql:chunk_search_index",
        )
        search_backend = getattr(self.retriever, "index", None)
        if search_backend and hasattr(search_backend, "trace_index_name"):
            retrieval_index = search_backend.trace_index_name()
        return self.repository.save_exchange(
            db,
            conversation=conversation,
            question=question,
            answer=validated.answer,
            answer_status=validated.status,
            model_id=model_id,
            trace_id=trace_id,
            citations=[source.model_dump() for source in validated.sources],
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
                "embedding_fingerprint": getattr(self.settings, "embedding_fingerprint", None),
                "evidence": [
                    {
                        "chunk_id": item.chunk_id,
                        "document_id": item.document_id,
                        "version_id": item.version_id,
                        "score": item.score,
                    }
                    for item in evidence
                ],
            },
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )

    def _validate_live_sources(
        self, db: Session, validated: ValidatedAnswer, evidence: list[Evidence]
    ) -> None:
        """落库前再次确认引用仍可搜索，阻止版本切换竞态产生陈旧引用。"""

        if not getattr(self.settings, "validate_citations_against_database", True):
            return
        cited = {citation for claim in validated.claims for citation in claim.citations}
        if not cited:
            return
        live = self.repository.live_searchable_chunk_ids(db, cited)
        if live != cited:
            raise AnswerValidationError("A cited source is no longer searchable")

    def _prepare_answer(
        self,
        db: Session,
        *,
        question: str,
        project_ids: list[str],
        pinned_model: str | None,
    ) -> AnswerPreparation:
        """完成查询理解、检索、路由和证据预算准备。"""

        if self.query_understanding is None:
            query_plan = fallback_query_plan(question, "service-not-configured")
            evidence = self.retriever.search(question, project_ids)
        else:
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
        if not evidence:
            return AnswerPreparation(query_plan, [], None, None)

        version_pairs: dict[str, set[str]] = {}
        for item in evidence:
            version_pairs.setdefault(item.document_id, set()).add(item.version_id)
        has_conflict = any(len(versions) > 1 for versions in version_pairs.values())
        tier = route_tier(
            question,
            [item.document_id for item in evidence[:6]],
            has_conflict,
        )
        evidence = fit_evidence_budget(evidence, 4000 if tier == "plus" else 7000)
        prompt = build_evidence_prompt(
            question,
            evidence,
            self.settings.prompt_version,
            query_plan.requested_facts,
        )
        return AnswerPreparation(query_plan, evidence, tier, prompt)

    def _generate_answer(
        self,
        db: Session,
        *,
        question: str,
        preparation: AnswerPreparation,
        pinned_model: str | None,
    ) -> GenerationResult:
        """最多尝试两次受约束生成，并封装升级、降级和纠错策略。"""

        tier = preparation.requested_tier
        user_prompt = preparation.user_prompt
        assert tier is not None and user_prompt is not None

        model_id: str | None = None
        attempted_tier = tier
        failure_kind = "validation"
        for attempt in range(2):
            # 第二次只提升非固定的 Plus 请求；固定模型评测必须保持可重复。
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
                validated = validate_answer(
                    parse_json_object(call.content),
                    preparation.evidence,
                )
                validated = attach_cross_document_bridges(
                    question,
                    validated,
                    preparation.evidence,
                )
                if validated.status == "insufficient_evidence":
                    validated.answer = refusal_text(question)
                self._validate_live_sources(db, validated, preparation.evidence)
                return GenerationResult(validated, model_id, attempted_tier)
            except NoModelAvailable:
                failure_kind = "provider"
                # Max 不可用时，允许一次仍受证据约束的 Plus 降级。
                if tier == "max" and attempt == 0 and not pinned_model:
                    logger.warning(
                        "Max tier unavailable; degrading one grounded answer attempt to Plus"
                    )
                    attempted_tier = "plus"
                    continue
                break
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Answer output rejected; retrying with citation correction (%s: %s)",
                    type(exc).__name__,
                    str(exc),
                )
                user_prompt += CITATION_CORRECTION_PROMPT
                if pinned_model:
                    continue
        return GenerationResult(None, model_id, attempted_tier, failure_kind)

    def _persist_response(
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
        preparation: AnswerPreparation,
        actual_tier: str | None,
    ) -> ChatResponse:
        """持久化审计记录并构造稳定的公开响应。"""

        message = self._persist(
            db,
            conversation=conversation,
            question=question,
            validated=validated,
            model_id=model_id,
            trace_id=trace_id,
            started_at=started_at,
            project_ids=project_ids,
            evidence=preparation.evidence,
            requested_tier=preparation.requested_tier,
            actual_tier=actual_tier,
            query_plan=preparation.query_plan,
        )
        return ChatResponse(
            status=validated.status,
            answer=validated.answer,
            claims=validated.claims,
            sources=validated.sources,
            model_id=model_id,
            route_tier=actual_tier,
            conversation_id=conversation.id,
            message_id=message.id,
            trace_id=trace_id,
        )

    def answer(
        self,
        db: Session,
        *,
        question: str,
        project_ids: list[str],
        conversation_id: str | None,
        pinned_model: str | None,
    ) -> ChatResponse:
        """完成一次证据约束问答，并保证失败路径也返回可审计结果。"""

        started_at = time.perf_counter()
        trace_id = str(uuid.uuid4())
        conversation = self.repository.get_or_create_conversation(db, conversation_id)
        preparation = self._prepare_answer(
            db,
            question=question,
            project_ids=project_ids,
            pinned_model=pinned_model,
        )

        if not preparation.evidence:
            # 无证据时不调用模型，直接返回语言匹配的确定性拒答。
            validated = ValidatedAnswer(
                status="insufficient_evidence",
                answer=refusal_text(question),
                claims=[],
                sources=[],
            )
            return self._persist_response(
                db,
                conversation=conversation,
                question=question,
                validated=validated,
                model_id=None,
                trace_id=trace_id,
                started_at=started_at,
                project_ids=project_ids,
                preparation=preparation,
                actual_tier=None,
            )

        generation = self._generate_answer(
            db,
            question=question,
            preparation=preparation,
            pinned_model=pinned_model,
        )
        if generation.validated is not None:
            return self._persist_response(
                db,
                conversation=conversation,
                question=question,
                validated=generation.validated,
                model_id=generation.model_id,
                trace_id=trace_id,
                started_at=started_at,
                project_ids=project_ids,
                preparation=preparation,
                actual_tier=generation.actual_tier,
            )

        refusal = ValidatedAnswer(
            status="insufficient_evidence",
            answer=refusal_text(
                question,
                validation_failed=generation.failure_kind == "validation",
                generation_unavailable=generation.failure_kind == "provider",
            ),
            claims=[],
            sources=[],
        )
        return self._persist_response(
            db,
            conversation=conversation,
            question=question,
            validated=refusal,
            model_id=generation.model_id,
            trace_id=trace_id,
            started_at=started_at,
            project_ids=project_ids,
            preparation=preparation,
            actual_tier=generation.actual_tier,
        )
