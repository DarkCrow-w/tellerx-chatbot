"""Application service that orchestrates retrieval, generation, and persistence."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.contracts.schemas import ChatResponse
from app.core.config import Settings
from app.db.models import Conversation, Message
from app.integrations.openai_client import ChatCallResult, parse_json_object
from app.knowledge.evidence import Evidence
from app.knowledge.search_text import normalize_query
from app.repositories.chat import ChatRepository
from app.services.answer_bridges import attach_cross_document_bridges
from app.services.answer_contract import (
    SYSTEM_PROMPT,
    AnswerValidationError,
    ValidatedAnswer,
    build_evidence_prompt,
    fit_evidence_budget,
    refusal_text,
    validate_answer,
)
from app.services.answer_progress import progress
from app.services.model_router import NoModelAvailable, route_tier
from app.services.query_understanding import (
    QueryPlan,
    QueryUnderstandingService,
    fallback_query_plan,
)
from app.services.retrieval import RetrievalOutcome, retrieval_diagnostics

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
    retrieval_intent: str = "global_lookup"
    resolved_document: dict | None = None
    resolved_scope: str = "global"
    retrieval_confidence: float = 1.0
    clarification_options: list[dict] | None = None
    failure_reason: str | None = None


@dataclass(slots=True)
class GenerationResult:
    """生成尝试的结果；失败时 ``validated`` 为空并记录失败类别。"""

    validated: ValidatedAnswer | None
    model_id: str | None
    actual_tier: str | None
    failure_kind: str = "validation"


CITATION_CORRECTION_PROMPT = """

PREVIOUS_OUTPUT_REJECTED: Return a fresh JSON object. Copy each quote exactly and
contiguously from one evidence block. Do not paraphrase inside quote fields. Return
at most 6 claims, use the shortest sufficient quote for each claim, and keep the JSON
compact. Prioritize the most important supported facts instead of producing an
exhaustive answer. For cross-document joins, cite the subject-to-identifier bridge
evidence together with the downstream value evidence.
"""


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
        retrieval_intent: str,
        resolved_document: dict | None,
        resolved_scope: str,
        retrieval_confidence: float,
        clarification_options: list[dict],
        failure_reason: str | None,
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
        citations = [source.model_dump() for source in validated.sources]
        normalized_query = normalize_query(question)
        retrieval_trace = {
            "prompt_version": getattr(self.settings, "prompt_version", None),
            "routing": {
                "requested_tier": requested_tier,
                "actual_tier": actual_tier,
            },
            "query_understanding": query_plan.as_trace_dict(),
            "candidate_stages": retrieval_diagnostics(),
            "scope_resolution": {
                "retrieval_intent": retrieval_intent,
                "resolved_document": resolved_document,
                "resolved_scope": resolved_scope,
                "retrieval_confidence": retrieval_confidence,
                "clarification_options": clarification_options,
                "failure_reason": failure_reason,
            },
            "embedding_fingerprint": getattr(self.settings, "embedding_fingerprint", None),
            "evidence": [
                {
                    "chunk_id": item.chunk_id,
                    "document_id": item.document_id,
                    "version_id": item.version_id,
                    "section_id": item.section_id,
                    "breadcrumb": list(item.breadcrumb),
                    "score": item.score,
                }
                for item in evidence
            ],
        }
        return self.repository.save_exchange(
            db,
            conversation=conversation,
            question=question,
            answer=validated.answer,
            answer_status=validated.status,
            model_id=model_id,
            trace_id=trace_id,
            citations=citations,
            normalized_query=normalized_query,
            project_ids=project_ids,
            index_name=retrieval_index,
            retrieval_json=retrieval_trace,
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
        document_id: str | None = None,
        document_hint: str | None = None,
        section_path: list[str] | None = None,
    ) -> AnswerPreparation:
        """完成查询理解、检索、路由和证据预算准备。"""

        progress("understanding", "正在理解问题与查询范围")
        if self.query_understanding is None:
            query_plan = fallback_query_plan(question, "service-not-configured")
        else:
            query_plan = self.query_understanding.understand(
                db,
                question,
                pinned_model=pinned_model,
            )
        focus = list(dict.fromkeys((*query_plan.subjects, *query_plan.requested_facts)))[:3]
        progress(
            "retrieving",
            "正在检索相关文档和章节",
            ["查询重点：" + "、".join(focus)] if focus else None,
        )
        if hasattr(self.retriever, "search_with_scope"):
            outcome: RetrievalOutcome = self.retriever.search_with_scope(
                question,
                project_ids,
                query_plan=query_plan,
                document_id=document_id,
                document_hint=document_hint,
                section_path=section_path,
            )
            evidence = outcome.evidence
        else:
            if self.query_understanding is None:
                evidence = self.retriever.search(question, project_ids)
            else:
                evidence = self.retriever.search(question, project_ids, query_plan=query_plan)
            outcome = RetrievalOutcome(evidence=evidence)
        progress(
            "retrieved",
            f"检索得到 {len({item.document_id for item in evidence})} 份候选文档、{len(evidence)} 段参考内容",
            [
                "相关文档：" + name
                for name in list(dict.fromkeys(item.filename for item in evidence))[:2]
            ]
            + (
                ["相关章节：" + " › ".join(evidence[0].breadcrumb)]
                if evidence and evidence[0].breadcrumb
                else []
            ),
        )
        if not evidence:
            logger.info(
                "证据检索完成 evidence_count=0 project_count=%d",
                len(project_ids),
            )
            return AnswerPreparation(
                query_plan,
                [],
                None,
                None,
                retrieval_intent=outcome.retrieval_intent,
                resolved_document=outcome.resolved_document,
                resolved_scope=outcome.resolved_scope,
                retrieval_confidence=outcome.retrieval_confidence,
                clarification_options=outcome.clarification_options,
                failure_reason=outcome.failure_reason,
            )

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
        logger.info(
            "证据检索与路由完成 evidence_count=%d project_count=%d tier=%s conflict=%s",
            len(evidence),
            len(project_ids),
            tier,
            has_conflict,
        )
        return AnswerPreparation(
            query_plan,
            evidence,
            tier,
            prompt,
            retrieval_intent=outcome.retrieval_intent,
            resolved_document=outcome.resolved_document,
            resolved_scope=outcome.resolved_scope,
            retrieval_confidence=outcome.retrieval_confidence,
            clarification_options=outcome.clarification_options,
            failure_reason=outcome.failure_reason,
        )

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
            logger.info(
                "回答生成尝试 attempt=%d tier=%s pinned=%s evidence_count=%d",
                attempt + 1,
                attempted_tier,
                bool(pinned_model),
                len(preparation.evidence),
            )
            progress(
                "generating",
                "正在根据参考内容整理答案" if attempt == 0 else "正在重新整理答案并核对引用",
            )
            try:
                call = self.router.call(
                    db,
                    tier=attempted_tier,
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    pinned_model=pinned_model,
                    prompt_version=self.settings.prompt_version,
                )
                progress("validating", "正在核对答案与原文引用")
                model_id = call.model_id
                payload = parse_json_object(call.content)
                validated = validate_answer(
                    payload,
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
                missing = payload.get("unanswered_fields", [])
                if validated.status == "answered" and isinstance(missing, list):
                    labels = [
                        fact for fact in preparation.query_plan.requested_facts if fact in missing
                    ]
                    if labels:
                        prefix = (
                            "Missing evidence for: "
                            if preparation.query_plan.language == "en"
                            else "以下问题项未找到充分证据："
                        )
                        validated.answer += "\n" + prefix + "、".join(labels)
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
            retrieval_intent=preparation.retrieval_intent,
            resolved_document=preparation.resolved_document,
            resolved_scope=preparation.resolved_scope,
            retrieval_confidence=preparation.retrieval_confidence,
            clarification_options=preparation.clarification_options or [],
            failure_reason=preparation.failure_reason,
            started_at=started_at,
            project_ids=project_ids,
            evidence=preparation.evidence,
            requested_tier=preparation.requested_tier,
            actual_tier=actual_tier,
            query_plan=preparation.query_plan,
        )
        response = ChatResponse(
            status=validated.status,
            answer=validated.answer,
            claims=validated.claims,
            sources=validated.sources,
            model_id=model_id,
            route_tier=actual_tier,
            conversation_id=conversation.id,
            message_id=message.id,
            trace_id=trace_id,
            retrieval_intent=preparation.retrieval_intent,
            resolved_document=preparation.resolved_document,
            resolved_scope=preparation.resolved_scope,
            retrieval_confidence=preparation.retrieval_confidence,
            clarification_options=preparation.clarification_options or [],
        )
        logger.info(
            "问答处理完成 trace_id=%s status=%s model=%s tier=%s evidence_count=%d "
            "source_count=%d elapsed_ms=%.1f",
            trace_id,
            validated.status,
            model_id or "none",
            actual_tier or "none",
            len(preparation.evidence),
            len(validated.sources),
            (time.perf_counter() - started_at) * 1000,
        )
        return response

    def answer(
        self,
        db: Session,
        *,
        question: str,
        project_ids: list[str],
        conversation_id: str | None,
        pinned_model: str | None,
        document_id: str | None = None,
        document_hint: str | None = None,
        section_path: list[str] | None = None,
    ) -> ChatResponse:
        """完成一次证据约束问答，并保证失败路径也返回可审计结果。"""

        started_at = time.perf_counter()
        trace_id = str(uuid.uuid4())
        logger.info(
            "问答处理开始 trace_id=%s question_chars=%d project_count=%d pinned=%s",
            trace_id,
            len(question),
            len(project_ids),
            bool(pinned_model),
        )
        conversation = self.repository.get_or_create_conversation(db, conversation_id)
        preparation = self._prepare_answer(
            db,
            question=question,
            project_ids=project_ids,
            pinned_model=pinned_model,
            document_id=document_id,
            document_hint=document_hint,
            section_path=section_path,
        )

        if preparation.evidence:
            generation = self._generate_answer(
                db,
                question=question,
                preparation=preparation,
                pinned_model=pinned_model,
            )
            validated = generation.validated or self._generation_refusal(question, generation)
        else:
            # 范围不明确或没有证据时，无需请求生成模型。
            generation = GenerationResult(None, None, None)
            validated = self._retrieval_refusal(question, preparation.failure_reason)

        progress("saving", "正在保存问答结果")
        return self._persist_response(
            db,
            conversation=conversation,
            question=question,
            validated=validated,
            model_id=generation.model_id,
            trace_id=trace_id,
            started_at=started_at,
            project_ids=project_ids,
            preparation=preparation,
            actual_tier=generation.actual_tier,
        )

    @staticmethod
    def _retrieval_refusal(question: str, failure_reason: str | None) -> ValidatedAnswer:
        status = "insufficient_evidence"
        if failure_reason == "ambiguous_document":
            status = "clarification_required"
            answer = "匹配到多份名称相近的文档，请先选择要查询的文档。"
        elif failure_reason == "document_not_found":
            answer = "未找到指定的当前有效文档，因此没有从其他文档拼凑答案。"
        else:
            answer = refusal_text(question)
        return ValidatedAnswer(status=status, answer=answer, claims=[], sources=[])

    @staticmethod
    def _generation_refusal(question: str, generation: GenerationResult) -> ValidatedAnswer:
        return ValidatedAnswer(
            status="insufficient_evidence",
            answer=refusal_text(
                question,
                validation_failed=generation.failure_kind == "validation",
                generation_unavailable=generation.failure_kind == "provider",
            ),
            claims=[],
            sources=[],
        )
