"""Hybrid retrieval policy built on search and Qwen adapter ports."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.integrations.openai_client import ModelAPIError, OpenAIModelClient
from app.integrations.search import SearchIndex
from app.knowledge.evidence import Evidence
from app.knowledge.search_text import (
    EXACT_IDENTIFIER,
    _query_subject_signals,
    normalize_query,
)

if TYPE_CHECKING:
    from app.services.query_understanding import QueryPlan

from app.services import business_queries, candidate_ranking, candidate_selection

logger = logging.getLogger(__name__)
_RETRIEVAL_DIAGNOSTICS: ContextVar[list[dict] | None] = ContextVar(
    "retrieval_diagnostics", default=None
)


def retrieval_diagnostics() -> list[dict]:
    return list(_RETRIEVAL_DIAGNOSTICS.get() or [])


@dataclass(slots=True)
class RetrievalOutcome:
    """检索证据以及可向产品层解释的文档范围决策。"""

    evidence: list[Evidence] = field(default_factory=list)
    retrieval_intent: str = "global_lookup"
    resolved_document: dict[str, Any] | None = None
    resolved_scope: str = "global"
    retrieval_confidence: float = 1.0
    clarification_options: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None


class Retriever:
    """编排确定性的混合召回，并在候选集稳定后按需调用千问重排。"""

    def __init__(self, settings: Settings, index: SearchIndex, model_client: OpenAIModelClient):
        """注入检索配置、搜索后端和向量/重排客户端。"""

        self.settings = settings
        self.index = index
        self.model_client = model_client
        self._query_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()

    def _query_embedding(self, query: str) -> list[float]:
        """获取查询向量，并按模型指纹维护带 TTL 的进程内 LRU 缓存。"""

        normalized = normalize_query(query)
        key = hashlib.sha256(
            f"{self.settings.embedding_fingerprint}:{normalized}".encode()
        ).hexdigest()
        now = time.monotonic()
        cached = self._query_cache.get(key)
        if cached and now - cached[0] <= self.settings.query_embedding_cache_ttl_seconds:
            self._query_cache.move_to_end(key)
            return cached[1]
        if cached:
            self._query_cache.pop(key, None)
        embeddings, _ = self.model_client.embeddings([normalized])
        if len(embeddings) != 1:
            raise ValueError("Query embedding response count does not match input")
        if len(self._query_cache) >= self.settings.query_embedding_cache_size:
            self._query_cache.popitem(last=False)
        self._query_cache[key] = (now, embeddings[0])
        return embeddings[0]

    def _select_rrf_evidence(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        related_hits: list[dict[str, Any]],
        linked_identifiers: list[str] | None = None,
    ) -> list[Evidence]:
        """在禁用或无法使用 Rerank 时，按 RRF 顺序完成邻居与来源桥接。"""

        selected_rows = candidate_selection.attach_short_chunk_neighbors(
            candidates[: self.settings.evidence_top_k],
            related_hits,
            max_extra=max(1, self.settings.evidence_top_k // 2),
        )
        selected_rows = candidate_selection.attach_provenance_bridge_chunks(
            query,
            selected_rows,
            related_hits,
            linked_identifiers or [],
            max_extra=2,
        )
        return [
            candidate_ranking.to_evidence(row["hit"]["_source"], float(row.get("score") or 0.0))
            for row in selected_rows
        ]

    def _rank_candidates(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
        related_hits: list[dict[str, Any]],
        linked_identifiers: list[str],
        semantic_context: str,
        semantic_anchors: tuple[str, ...],
    ) -> list[Evidence]:
        """按配置选择 RRF 或专用重排；任何重排故障都安全回退到 RRF。"""

        if not getattr(self.settings, "rerank_enabled", False):
            return self._select_rrf_evidence(
                query,
                candidates,
                related_hits,
                linked_identifiers,
            )
        passages = [candidate_ranking.rerank_passage(row) for row in candidates]
        rerank_query = "\n".join(
            part
            for part in [
                query,
                semantic_context,
                (
                    "Approved cross-document references: " + " ".join(linked_identifiers)
                    if linked_identifiers
                    else ""
                ),
            ]
            if part
        )
        rerank_top_n = min(
            len(candidates),
            max(self.settings.evidence_top_k * 3, self.settings.evidence_top_k),
        )
        try:
            ranked = self.model_client.rerank(rerank_query, passages, rerank_top_n)
        except ModelAPIError as exc:
            logger.warning("Rerank unavailable; using RRF ordering: %s", exc.code)
            return self._select_rrf_evidence(
                query,
                candidates,
                related_hits,
                linked_identifiers,
            )
        ranked = candidate_ranking.ensure_signal_coverage(
            query,
            candidates,
            ranked,
            rerank_top_n,
            additional_signals=semantic_anchors,
        )
        ranked = candidate_ranking.diversify_documents(
            candidates, ranked, self.settings.evidence_top_k, query=query
        )
        selected_rows = [
            {**candidates[index], "score": score}
            for index, score in ranked[: self.settings.evidence_top_k]
            if 0 <= index < len(candidates)
        ]
        selected_rows = candidate_selection.attach_short_chunk_neighbors(
            selected_rows,
            related_hits,
            max_extra=max(1, self.settings.evidence_top_k // 2),
        )
        selected_rows = candidate_selection.attach_provenance_bridge_chunks(
            query,
            selected_rows,
            related_hits,
            linked_identifiers,
            max_extra=2,
        )
        return [
            candidate_ranking.to_evidence(row["hit"]["_source"], float(row.get("score") or 0.0))
            for row in selected_rows
        ]

    def _retrieve_for_statuses(
        self,
        query: str,
        project_ids: list[str],
        statuses: list[str],
        principal_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """对指定文档状态执行词法与向量召回，并在允许时降级为纯 BM25。"""

        scope_kwargs = {"document_ids": document_ids} if document_ids is not None else {}
        lexical = self.index.lexical_search(
            query,
            project_ids,
            statuses,
            self.settings.retrieval_top_k,
            principal_ids,
            **scope_kwargs,
        )
        vector_hits: list[dict[str, Any]] = []
        try:
            vector = self._query_embedding(query)
            vector_hits = self.index.vector_search(
                vector,
                project_ids,
                statuses,
                self.settings.retrieval_top_k,
                principal_ids,
                **scope_kwargs,
            )
        except ModelAPIError as exc:
            if not self.settings.allow_bm25_only:
                raise
            logger.warning(
                "查询向量服务失败，使用关键词召回 status=%s code=%s lexical_hits=%d",
                exc.status_code, exc.code, len(lexical),
            )
        fused = candidate_ranking.rrf(lexical, vector_hits)
        logger.debug(
            "召回通道完成 project_count=%d statuses=%s lexical=%d vector=%d fused=%d",
            len(project_ids),
            ",".join(statuses),
            len(lexical),
            len(vector_hits),
            len(fused),
        )
        return fused

    def _retrieve_linked_identifier_rows(
        self,
        identifiers: list[str],
        project_ids: list[str],
        statuses: list[str],
        principal_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """用精确词法查询逐个扩展已获证据支持的跨文档标识。

        长语义查询容易淹没只含英文标识的下游文档；这些 ID 已由召回证据落地，
        因此可以确定性扩展，无需再次调用模型或向量接口。
        """

        channels: list[list[dict[str, Any]]] = []
        seen: set[str] = set()
        for identifier in identifiers:
            normalized = identifier.upper()
            if normalized.casefold() in seen or not EXACT_IDENTIFIER.fullmatch(normalized):
                continue
            seen.add(normalized.casefold())
            scope_kwargs = {"document_ids": document_ids} if document_ids is not None else {}
            hits = self.index.lexical_search(
                normalized,
                project_ids,
                statuses,
                max(4, self.settings.evidence_top_k),
                principal_ids,
                **scope_kwargs,
            )
            if hits:
                channels.append(hits)
            if len(channels) >= 10:
                break
        return candidate_ranking.rrf(*channels) if channels else []

    def search(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None = None,
        query_plan: QueryPlan | None = None,
    ) -> list[Evidence]:
        """同时执行原问题与语义规划检索，并以原问题召回作为安全基线。"""

        if query_plan is None:
            result = self._search_once(query, project_ids, principal_ids, None)
            logger.info(
                "检索完成 mode=baseline project_count=%d evidence_count=%d",
                len(project_ids),
                len(result),
            )
            return result
        if self.settings.business_retrieval_enabled:
            # _search_once merges original-query, planned and business-fact candidates
            # before applying one shared subject decision and final evidence budget.
            return self._search_once(query, project_ids, principal_ids, query_plan)
        baseline = self._search_once(query, project_ids, principal_ids, None)
        planned = self._search_once(query, project_ids, principal_ids, query_plan)
        result = candidate_ranking.merge_evidence_channels(
            baseline,
            planned,
            limit=max(self.settings.evidence_top_k * 2, self.settings.evidence_top_k + 4),
        )
        logger.info(
            "检索完成 mode=semantic project_count=%d baseline=%d planned=%d evidence_count=%d",
            len(project_ids),
            len(baseline),
            len(planned),
            len(result),
        )
        return result

    def search_with_scope(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None = None,
        query_plan: QueryPlan | None = None,
        *,
        document_id: str | None = None,
        document_hint: str | None = None,
        section_path: list[str] | None = None,
    ) -> RetrievalOutcome:
        """先解析显式文档范围；没有可靠范围时完整保留全库检索。"""

        _RETRIEVAL_DIAGNOSTICS.set([])
        if not getattr(self.settings, "hierarchical_retrieval_enabled", True):
            return RetrievalOutcome(
                evidence=self.search(query, project_ids, principal_ids, query_plan)
            )
        hint = document_hint or (query_plan.document_hint if query_plan else None)
        resolved: dict[str, Any] | None = None
        if document_id:
            resolved = self.index.document_by_id(document_id, project_ids, principal_ids)
            if resolved is None:
                logger.info(
                    "文档范围解析失败 reason=document_not_found document_id=%s", document_id
                )
                return RetrievalOutcome(
                    retrieval_intent="document_lookup",
                    resolved_scope="document_not_found",
                    retrieval_confidence=0.0,
                    failure_reason="document_not_found",
                )
        elif hint:
            candidates = self.index.document_candidates(hint, project_ids, principal_ids, limit=8)
            if not candidates:
                logger.info("文档范围解析失败 reason=document_not_found hint=%s", hint)
                return RetrievalOutcome(
                    retrieval_intent="document_lookup",
                    resolved_scope="document_not_found",
                    retrieval_confidence=0.0,
                    failure_reason="document_not_found",
                )
            top = candidates[0]
            top_score = float(top["score"])
            runner_up = float(candidates[1]["score"]) if len(candidates) > 1 else 0.0
            if (
                len(candidates) == 1
                or (top_score >= 0.98 and runner_up < 0.95)
                or top_score - runner_up >= 0.12
            ):
                resolved = top
            else:
                logger.info(
                    "文档范围需要澄清 hint=%s candidate_count=%d top_score=%.3f",
                    hint,
                    len(candidates),
                    top_score,
                )
                return RetrievalOutcome(
                    retrieval_intent="ambiguous",
                    resolved_scope="clarification_required",
                    retrieval_confidence=top_score,
                    clarification_options=candidates[:5],
                    failure_reason="ambiguous_document",
                )
        if resolved is None:
            global_intent = (
                query_plan.retrieval_intent
                if query_plan and query_plan.retrieval_intent in {"global_lookup", "cross_source"}
                else "global_lookup"
            )
            return RetrievalOutcome(
                evidence=self.search(query, project_ids, principal_ids, query_plan),
                retrieval_intent=global_intent,
            )

        scoped_query = (
            query_plan.document_question if query_plan and query_plan.document_question else query
        )
        hints = tuple(section_path or (query_plan.section_hints if query_plan else ()))
        evidence = self._search_once(
            scoped_query,
            project_ids,
            principal_ids,
            query_plan,
            document_ids=[str(resolved["document_id"])],
            section_hints=hints,
        )
        resolved_id = str(resolved["document_id"])
        if any(item.document_id != resolved_id for item in evidence):
            logger.error("文档范围外证据已拦截 document_id=%s", resolved_id)
            return RetrievalOutcome(
                retrieval_intent="document_lookup",
                resolved_document=resolved,
                resolved_scope=str(resolved.get("filename") or resolved_id),
                retrieval_confidence=float(resolved.get("score") or 1.0),
                failure_reason="scope_violation",
            )
        logger.info(
            "文档范围解析完成 document_id=%s filename=%s evidence_count=%d",
            resolved_id,
            resolved.get("filename"),
            len(evidence),
        )
        return RetrievalOutcome(
            evidence=evidence,
            retrieval_intent="document_lookup",
            resolved_document=resolved,
            resolved_scope=str(resolved.get("filename") or resolved_id),
            retrieval_confidence=float(resolved.get("score") or 1.0),
        )

    def _business_channel(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None,
        query_plan: QueryPlan | None,
        fused: list[dict[str, Any]],
        document_ids: list[str] | None,
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], list[str]]:
        """在业务文档组内逐项查询事实，再与全库候选合并。"""
        subjects = business_queries.subject_signals(query, query_plan)
        resolved_subjects = list(subjects)
        groups = list(document_ids or [])
        discovery = getattr(self.index, "business_documents", None)
        if document_ids is None and callable(discovery):
            for subject in subjects[:3]:
                found = discovery(subject, project_ids, ["approved"], principal_ids, limit=10)
                if isinstance(found, list) and not found:
                    repaired = business_queries.grounded_subject_prefix(subject, fused, query_plan)
                    if repaired:
                        found = discovery(
                            repaired, project_ids, ["approved"], principal_ids, limit=10
                        )
                        if isinstance(found, list) and found:
                            resolved_subjects[resolved_subjects.index(subject)] = repaired
                if isinstance(found, list):
                    groups.extend(found)
        original_subjects = subjects
        subjects = tuple(resolved_subjects)
        groups = list(dict.fromkeys(groups))[:30]
        if not groups:
            return fused, subjects, groups
        facts = business_queries.fact_queries(query, query_plan, subjects)
        for fact in facts:
            recalled = self._retrieve_for_statuses(
                fact, project_ids, ["approved"], principal_ids, groups
            )
            fused = candidate_ranking.merge_fused(fused, recalled)
        trace = _RETRIEVAL_DIAGNOSTICS.get()
        if trace is not None:
            trace.append(
                {
                    "stage": "business_facts",
                    "subjects": list(subjects),
                    "original_subjects": list(original_subjects),
                    "document_ids": groups,
                    "queries": facts,
                }
            )
        return fused, subjects, groups

    def _prioritize_facts(
        self, rows: list[dict[str, Any]], query_plan: QueryPlan | None
    ) -> list[dict[str, Any]]:
        """为不同问题项预留证据，其余候选继续保留原有排名。"""
        if not rows or not query_plan:
            return rows
        selected = []
        covered = []
        for fact in query_plan.requested_facts[:6]:
            best = max(
                rows, key=lambda row: (business_queries.fact_coverage(row, fact), row["score"])
            )
            coverage = business_queries.fact_coverage(best, fact)
            covered.append(
                {
                    "fact": fact,
                    "lexical_coverage": coverage,
                    "chunk_id": best["hit"]["_source"].get("chunk_id") if coverage >= 0.5 else None,
                }
            )
            if coverage >= 0.5 and best not in selected:
                selected.append(best)
        trace = _RETRIEVAL_DIAGNOSTICS.get()
        if trace is not None:
            trace.append({"stage": "fact_coverage", "facts": covered})
        return selected + [row for row in rows if row not in selected]

    @staticmethod
    def _trace_candidates(stage: str, rows: list[dict[str, Any]]) -> None:
        trace = _RETRIEVAL_DIAGNOSTICS.get()
        if trace is not None:
            trace.append(
                {
                    "stage": stage,
                    "count": len(rows),
                    "candidates": [
                        {
                            "chunk_id": r["hit"]["_source"].get("chunk_id"),
                            "document_id": r["hit"]["_source"].get("document_id"),
                            "rank": i + 1,
                        }
                        for i, r in enumerate(rows[:60])
                    ],
                }
            )

    def _related_evidence(
        self,
        fused: list[dict[str, Any]],
        project_ids: list[str],
        principal_ids: list[str] | None,
        enabled: bool,
    ) -> list[dict[str, Any]]:
        """优先读取命中位置附近的分块；旧适配器沿用文档级扩展。"""
        related_document_ids = list(
            dict.fromkeys(
                str(row["hit"].get("_source", {}).get("document_id") or "")
                for row in fused
                if row["hit"].get("_source", {}).get("document_id")
            )
        )[: self.settings.evidence_top_k * 2]
        related_hits: list[dict[str, Any]] = []
        neighbors = getattr(self.index, "evidence_neighbors", None)
        if related_document_ids and not (enabled and callable(neighbors)):
            related_hits = self.index.document_chunks(
                related_document_ids,
                project_ids,
                ["approved"],
                max(
                    self.settings.rerank_candidates * 4,
                    len(related_document_ids) * 12,
                ),
                principal_ids,
            )
        if enabled and callable(neighbors):
            local = neighbors(
                [
                    str(row["hit"]["_source"]["chunk_id"])
                    for row in fused[: self.settings.evidence_top_k]
                ],
                project_ids,
                ["approved"],
                principal_ids,
            )
            if isinstance(local, list):
                related_hits = local
        return related_hits

    def _link_seeds(
        self,
        fused: list[dict[str, Any]],
        query_plan: QueryPlan | None,
        enabled: bool,
    ) -> list[dict[str, Any]]:
        seeds = fused
        if enabled and query_plan and query_plan.requested_facts:
            seeds = [
                row
                for row in fused
                if any(
                    business_queries.fact_coverage(row, fact) >= 0.5
                    for fact in query_plan.requested_facts
                )
            ]
        return seeds

    def _search_once(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None,
        query_plan: QueryPlan | None,
        *,
        document_ids: list[str] | None = None,
        section_hints: tuple[str, ...] = (),
    ) -> list[Evidence]:
        """执行一次完整检索：召回、关联扩展、重排、覆盖修复与证据整形。"""

        query = normalize_query(query)
        fused = self._retrieve_for_statuses(
            query, project_ids, ["approved"], principal_ids, document_ids
        )
        fused = self._recall_planned_queries(
            query, project_ids, principal_ids, query_plan, document_ids, fused
        )
        if section_hints:
            fused = candidate_ranking.boost_section_matches(fused, section_hints)
        enabled = self.settings.business_retrieval_enabled
        subjects = business_queries.subject_signals(query, query_plan)
        subject_documents = []
        if enabled:
            fused, subjects, subject_documents = self._business_channel(
                query, project_ids, principal_ids, query_plan, fused, document_ids
            )
        semantic_anchors = ()
        if enabled:
            semantic_anchors = subjects
        elif query_plan:
            semantic_anchors = query_plan.anchor_signals
        fused = candidate_ranking.boost_anchor_matches(fused, semantic_anchors)
        subject_is_ungrounded = (
            query_plan
            and subjects
            and document_ids is None
            and not subject_documents
            and not candidate_selection.has_grounded_subject(fused, subjects)
        )
        if subject_is_ungrounded:
            logger.info("Semantic subject was not grounded in any candidate; abstaining")
            return []
        seeds = self._link_seeds(fused, query_plan, enabled)
        linked_identifiers = candidate_selection.discover_linked_identifiers(
            query,
            seeds,
            anchor_signals=semantic_anchors or None,
        )
        semantic_context = query_plan.rerank_context() if query_plan else ""
        if linked_identifiers:
            fused = self._recall_references(
                query,
                semantic_context,
                linked_identifiers,
                project_ids,
                principal_ids,
                document_ids,
                fused,
            )
        fused = candidate_ranking.deduplicate_content(fused)
        fused = candidate_selection.enforce_exact_identifiers(query, fused, linked_identifiers)
        self._trace_candidates("before_subject_filter", fused)
        if document_ids is None:
            fused = candidate_selection.prefer_complete_entity_matches(
                query,
                fused,
                linked_identifiers,
                subject_signals=subjects if enabled else None,
                subject_document_ids=tuple(subject_documents),
            )
        self._trace_candidates("after_subject_filter", fused)
        if enabled:
            fused = self._prioritize_facts(fused, query_plan)
        related_hits = self._related_evidence(fused, project_ids, principal_ids, enabled)
        candidates = candidate_selection.select_rerank_candidate_pool(
            fused,
            related_hits,
            limit=self.settings.rerank_candidates,
            expansion_slots=self.settings.evidence_top_k,
            query=query,
        )
        self._trace_candidates("final_candidates", candidates)
        if not candidates:
            return []
        return self._rank_candidates(
            query=query,
            candidates=candidates,
            related_hits=related_hits,
            linked_identifiers=linked_identifiers,
            semantic_context=semantic_context,
            semantic_anchors=semantic_anchors,
        )

    def _recall_planned_queries(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None,
        query_plan: QueryPlan | None,
        document_ids: list[str] | None,
        fused: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """原问题召回之后，再合并查询计划给出的有限个改写。"""
        focus_terms = list(query_plan.subjects) if query_plan else _query_subject_signals(query)
        focus_query = " ".join(focus_terms)
        retrieval_queries = []
        if query_plan:
            retrieval_queries = list(query_plan.retrieval_queries)
        elif focus_query:
            retrieval_queries = [focus_query]
        for retrieval_query in retrieval_queries[:4]:
            retrieval_query = normalize_query(retrieval_query)
            if not retrieval_query or retrieval_query.casefold() == query.casefold():
                continue
            focused = self._retrieve_for_statuses(
                retrieval_query, project_ids, ["approved"], principal_ids, document_ids
            )
            fused = candidate_ranking.merge_fused(fused, focused)
        return fused

    def _recall_references(
        self,
        query: str,
        semantic_context: str,
        linked_identifiers: list[str],
        project_ids: list[str],
        principal_ids: list[str] | None,
        document_ids: list[str] | None,
        fused: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """沿已命中的编号补充原文，同时保持原有文档和权限范围。"""
        exact_expansion = self._retrieve_linked_identifier_rows(
            linked_identifiers,
            project_ids,
            ["approved"],
            principal_ids,
            document_ids,
        )
        fused = candidate_ranking.merge_fused(fused, exact_expansion)
        expanded_query = "\n".join(
            part
            for part in [
                query,
                semantic_context,
                "Approved cross-document references: " + " ".join(linked_identifiers),
            ]
            if part
        )
        expanded = self._retrieve_for_statuses(
            expanded_query, project_ids, ["approved"], principal_ids, document_ids
        )
        fused = candidate_ranking.merge_fused(fused, expanded)
        return fused
