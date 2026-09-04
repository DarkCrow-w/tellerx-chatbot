"""Hybrid retrieval policy built on search and Qwen adapter ports."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.integrations.openai_client import ModelAPIError, OpenAIModelClient
from app.integrations.search import (
    ACRONYM,
    CONTROLLED_ALIAS,
    EXACT_IDENTIFIER,
    SearchIndex,
    _lexical_signals,
    _query_subject_signals,
    _source_status,
    normalize_query,
)
from app.knowledge.evidence import Evidence

if TYPE_CHECKING:
    from app.services.query_understanding import QueryPlan

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _rrf(*ranked_lists: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
        """用倒数排名融合多个召回通道，避免不同分值尺度直接相加。"""

        combined: dict[str, dict[str, Any]] = {}
        for channel, ranked in enumerate(ranked_lists):
            for rank, hit in enumerate(ranked, start=1):
                chunk_id = hit.get("_source", {}).get("chunk_id") or hit.get("_id")
                if not chunk_id:
                    continue
                row = combined.setdefault(
                    chunk_id,
                    {"hit": hit, "score": 0.0, "channels": set(), "raw_scores": {}},
                )
                row["score"] += 1.0 / (k + rank)
                row["channels"].add(channel)
                row["raw_scores"][channel] = float(hit.get("_score") or 0.0)
        for row in combined.values():
            source = row["hit"].get("_source", {})
            if _source_status(source) == "approved":
                row["score"] *= 1.08
        return sorted(combined.values(), key=lambda row: row["score"], reverse=True)

    @staticmethod
    def _enforce_exact_identifiers(
        query: str,
        rows: list[dict[str, Any]],
        linked_identifiers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """强制结果覆盖问题中的全部精确标识符，防止部分命中误答。"""

        identifiers = {
            match.casefold()
            for pattern in (EXACT_IDENTIFIER, ACRONYM)
            for match in pattern.findall(normalize_query(query))
        }
        if not identifiers:
            return rows
        matched: list[dict[str, Any]] = []
        covered: set[str] = set()
        linked = {value.casefold() for value in linked_identifiers or []}
        for row in rows:
            searchable = Retriever._source_text(row)
            row_identifiers = {identifier for identifier in identifiers if identifier in searchable}
            if row_identifiers or any(identifier in searchable for identifier in linked):
                matched.append(row)
            covered.update(row_identifiers)
        return matched if covered == identifiers else []

    @classmethod
    def _discover_linked_identifiers(
        cls,
        query: str,
        rows: list[dict[str, Any]],
        limit: int = 12,
        anchor_signals: tuple[str, ...] | None = None,
    ) -> list[str]:
        """从已命中主题锚点中提取受控别名和跨文档引用标识。"""

        query_identifiers = {
            match.casefold()
            for pattern in (EXACT_IDENTIFIER, ACRONYM)
            for match in pattern.findall(normalize_query(query))
        }
        signals = [value.casefold() for value in (anchor_signals or tuple(_lexical_signals(query)))]
        anchors = [
            row for row in rows if any(signal in cls._source_text(row) for signal in signals)
        ]
        if not anchors:
            return []
        discovered: list[str] = []
        for row in anchors[:8]:
            for value in EXACT_IDENTIFIER.findall(cls._source_text(row)):
                normalized = value.casefold()
                if normalized not in query_identifiers and normalized not in {
                    item.casefold() for item in discovered
                }:
                    discovered.append(value.upper())
                    if len(discovered) >= limit:
                        return discovered
            source = row.get("hit", {}).get("_source", {})
            alias_text = "\n".join(
                str(source.get(field) or "") for field in ("title_path", "heading_path", "content")
            )
            for value in CONTROLLED_ALIAS.findall(alias_text):
                normalized = value.casefold().strip()
                if (
                    normalized
                    and normalized not in query_identifiers
                    and normalized not in {item.casefold() for item in discovered}
                ):
                    discovered.append(value.strip())
                    if len(discovered) >= limit:
                        return discovered
        return discovered

    @staticmethod
    def _merge_fused(
        primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """合并原问题与语义规划通道，并略微降低扩展通道权重。"""

        merged: dict[str, dict[str, Any]] = {}
        for channel, rows in enumerate((primary, secondary)):
            weight = 1.0 if channel == 0 else 0.92
            for row in rows:
                source = row.get("hit", {}).get("_source", {})
                chunk_id = str(source.get("chunk_id") or row.get("hit", {}).get("_id") or "")
                if not chunk_id:
                    continue
                if chunk_id not in merged:
                    merged[chunk_id] = {
                        **row,
                        "score": float(row.get("score") or 0.0) * weight,
                    }
                else:
                    merged[chunk_id]["score"] += float(row.get("score") or 0.0) * weight
        return sorted(merged.values(), key=lambda row: row["score"], reverse=True)

    @staticmethod
    def _merge_evidence_channels(
        baseline: list[Evidence], planned: list[Evidence], *, limit: int
    ) -> list[Evidence]:
        """保留原问题召回顺序，同时允许语义规划补充不重复的证据。"""

        merged: list[Evidence] = []
        seen: set[str] = set()
        for channel in (baseline, planned):
            for item in channel:
                if item.chunk_id in seen:
                    continue
                seen.add(item.chunk_id)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        return merged

    @classmethod
    def _boost_anchor_matches(
        cls, rows: list[dict[str, Any]], anchor_signals: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """提升命中主题锚点的候选，降低语义相似但主题错误的概率。"""

        signals = [value.casefold() for value in anchor_signals if value.strip()]
        if not signals:
            return rows
        boosted = []
        for row in rows:
            score = float(row.get("score") or 0.0)
            if any(signal in cls._source_text(row) for signal in signals):
                score *= 1.35
            boosted.append({**row, "score": score})
        return sorted(boosted, key=lambda row: row["score"], reverse=True)

    @classmethod
    def _has_grounded_subject(
        cls, rows: list[dict[str, Any]], subject_signals: tuple[str, ...]
    ) -> bool:
        """判断候选集中是否存在能直接落地问题主题的文本。"""

        signals = [value.casefold() for value in subject_signals if value.strip()]
        return bool(signals) and any(
            signal in cls._source_text(row) for row in rows for signal in signals
        )

    @staticmethod
    def _deduplicate_content(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按内容哈希去重，避免相同段落挤占证据预算。"""

        unique = []
        seen: set[str] = set()
        for row in rows:
            source = row.get("hit", {}).get("_source", {})
            key = str(source.get("content_hash") or source.get("chunk_id") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(row)
        return unique

    @classmethod
    def _select_rerank_candidate_pool(
        cls,
        fused: list[dict[str, Any]],
        related_hits: list[dict[str, Any]],
        *,
        limit: int,
        expansion_slots: int,
        query: str,
    ) -> list[dict[str, Any]]:
        """为已证明相关文档的相邻分块预留重排名额。"""

        if limit <= 0:
            return []
        reserve = min(max(0, expansion_slots), max(0, limit - 1))
        base_count = max(1, limit - reserve)
        base = fused[:base_count]
        seen = {str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") for row in base}
        origins: dict[str, list[int]] = {}
        document_order: list[str] = []
        for row in base:
            source = row.get("hit", {}).get("_source", {})
            document_id = str(source.get("document_id") or "")
            if not document_id:
                continue
            if document_id not in origins:
                origins[document_id] = []
                document_order.append(document_id)
            origins[document_id].append(int(source.get("chunk_ordinal") or 0))

        by_document: dict[str, list[dict[str, Any]]] = {}
        for hit in related_hits:
            source = hit.get("_source", {})
            chunk_id = str(source.get("chunk_id") or "")
            document_id = str(source.get("document_id") or "")
            if not chunk_id or chunk_id in seen or document_id not in origins:
                continue
            by_document.setdefault(document_id, []).append(
                {
                    "hit": hit,
                    "score": 0.0,
                    "channels": {"document_expansion"},
                    "raw_scores": {},
                }
            )

        expanded: list[dict[str, Any]] = []
        for document_id in document_order:
            options = by_document.get(document_id, [])
            if not options or len(expanded) >= reserve:
                continue
            origin_ordinals = origins[document_id]
            selected = min(
                options,
                key=lambda row: (
                    min(
                        abs(int(row["hit"].get("_source", {}).get("chunk_ordinal") or 0) - ordinal)
                        for ordinal in origin_ordinals
                    ),
                    -cls._authority_quality(query, row),
                    int(row["hit"].get("_source", {}).get("chunk_ordinal") or 0),
                ),
            )
            expanded.append(selected)
            seen.add(str(selected["hit"].get("_source", {}).get("chunk_id") or ""))

        remaining = [
            row
            for row in fused[base_count:]
            if str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") not in seen
        ]
        return cls._deduplicate_content([*base, *expanded, *remaining])[:limit]

    @classmethod
    def _attach_short_chunk_neighbors(
        cls,
        selected: list[dict[str, Any]],
        related_hits: list[dict[str, Any]],
        *,
        max_extra: int = 4,
        short_threshold: int = 180,
    ) -> list[dict[str, Any]]:
        """当选中分块过短、疑似只有标题时，补充最近的正文值块。"""

        if max_extra <= 0:
            return selected
        seen = {
            str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") for row in selected
        }
        extras: list[dict[str, Any]] = []
        for row in selected:
            if len(extras) >= max_extra:
                break
            source = row.get("hit", {}).get("_source", {})
            if len(str(source.get("content") or "").strip()) >= short_threshold:
                continue
            document_id = str(source.get("document_id") or "")
            origin = int(source.get("chunk_ordinal") or 0)
            options = [
                hit
                for hit in related_hits
                if str(hit.get("_source", {}).get("document_id") or "") == document_id
                and str(hit.get("_source", {}).get("chunk_id") or "") not in seen
            ]
            if not options:
                continue
            hit = min(
                options,
                key=lambda candidate: (
                    abs(int(candidate.get("_source", {}).get("chunk_ordinal") or 0) - origin),
                    int(candidate.get("_source", {}).get("chunk_ordinal") or 0) < origin,
                ),
            )
            wrapped = {
                "hit": hit,
                "score": float(row.get("score") or 0.0) * 0.99,
                "channels": {"selected_neighbor"},
                "raw_scores": {},
            }
            extras.append(wrapped)
            seen.add(str(hit.get("_source", {}).get("chunk_id") or ""))
        return cls._deduplicate_content([*selected, *extras])

    @classmethod
    def _attach_provenance_bridge_chunks(
        cls,
        query: str,
        selected: list[dict[str, Any]],
        related_hits: list[dict[str, Any]],
        linked_identifiers: list[str],
        *,
        max_extra: int = 2,
    ) -> list[dict[str, Any]]:
        """在下游事实旁保留“主题→引用标识”的来源桥接分块。

        重排器天然偏好含最终值的段落，跨文档查询时可能因此丢掉证明二者关系的
        需求或注册表段落。这里仅保留已召回的确定性映射，不推断或创造新关系。
        """

        if max_extra <= 0 or not selected or not related_hits:
            return selected
        query_ids = {
            value.casefold()
            for pattern in (EXACT_IDENTIFIER, ACRONYM)
            for value in pattern.findall(normalize_query(query))
        }
        subject_signals = query_ids or {
            value.casefold()
            for value in (*_query_subject_signals(query), *_lexical_signals(query))
            if len(value.strip()) >= 2
        }
        downstream_ids = {
            value.casefold()
            for row in selected
            for value in EXACT_IDENTIFIER.findall(cls._source_text(row))
            if value.casefold() not in query_ids
        }
        downstream_ids.update(value.casefold() for value in linked_identifiers)
        if not subject_signals or not downstream_ids:
            return selected

        seen = {
            str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "") for row in selected
        }
        preferred_types = {
            "business-requirement",
            "requirement",
            "terminology-registry",
            "mapping",
            "reference-index",
        }
        options: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
        for position, hit in enumerate(related_hits):
            source = hit.get("_source", {})
            chunk_id = str(source.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen or _source_status(source) != "approved":
                continue
            wrapped = {"hit": hit}
            text = cls._source_text(wrapped)
            matched_subjects = sum(signal in text for signal in subject_signals)
            matched_links = sum(identifier in text for identifier in downstream_ids)
            if not matched_subjects or not matched_links:
                continue
            document_type = str(source.get("document_type") or "").casefold()
            options.append(
                (
                    (
                        int(document_type in preferred_types),
                        matched_subjects,
                        matched_links,
                        -position,
                    ),
                    {
                        "hit": hit,
                        "score": 0.0,
                        "channels": {"provenance_bridge"},
                        "raw_scores": {},
                    },
                )
            )
        if not options:
            return selected
        extras = [row for _, row in sorted(options, key=lambda item: item[0], reverse=True)]
        return cls._deduplicate_content([*selected, *extras[:max_extra]])

    @classmethod
    def _prefer_complete_entity_matches(
        cls,
        query: str,
        rows: list[dict[str, Any]],
        linked_identifiers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """单一实体查询只保留完整实体或已验证关联标识的候选。"""

        entity_signals = _query_subject_signals(query)
        if len(entity_signals) != 1:
            return rows
        signal = entity_signals[0].casefold()
        if signal in {"业务", "系统", "项目", "模块", "规则", "接口", "文档", "策略"}:
            return rows
        linked = {value.casefold() for value in linked_identifiers or []}
        return [
            row
            for row in rows
            if signal in cls._source_text(row)
            or any(identifier in cls._source_text(row) for identifier in linked)
        ]

    @staticmethod
    def _authority_quality(query: str, row: dict[str, Any]) -> int:
        """按问题意图评价时效权威性，默认优先正式当前值而非历史描述。"""

        normalized_query = normalize_query(query).casefold()
        text = Retriever._source_text(row)
        authoritative = (
            "approved",
            "current",
            "effective date",
            "signed",
            "authoritative",
            "正式",
            "当前",
            "生效",
            "已批准",
        )
        historical = (
            "not the approval page",
            "not current",
            "not the final decision",
            "retired",
            "historical",
            "superseded",
            "deprecated",
            "draft",
            "candidate",
            "never approved",
            "旧值",
            "退役",
            "历史",
            "草稿",
            "候选",
            "作废",
        )
        asks_for_history = any(
            term in normalized_query for term in ("历史", "旧值", "过去", "退役")
        ) or bool(re.search(r"\b(?:historical|old|retired)\b", normalized_query))
        if asks_for_history:
            return 2 * sum(term in text for term in historical) - 2 * sum(
                term in text for term in authoritative
            )
        return 2 * sum(term in text for term in authoritative) - 3 * sum(
            term in text for term in historical
        )

    @staticmethod
    def _diversify_documents(
        candidates: list[dict[str, Any]],
        ranked: list[tuple[int, float]],
        top_k: int,
        query: str = "",
    ) -> list[tuple[int, float]]:
        """先为每份文档保留一个权威代表，再追加同文档的其他候选。"""

        valid = [(index, score) for index, score in ranked if 0 <= index < len(candidates)]
        seen_indexes: set[int] = set()
        document_order: list[str] = []
        by_document: dict[str, list[tuple[int, tuple[int, float]]]] = {}
        deduplicated: list[tuple[int, float]] = []
        for position, item in enumerate(valid):
            index = item[0]
            if index in seen_indexes:
                continue
            seen_indexes.add(index)
            deduplicated.append(item)
            source = candidates[index]["hit"].get("_source", {})
            document_id = str(source.get("document_id") or source.get("filename") or index)
            if document_id not in by_document:
                document_order.append(document_id)
                by_document[document_id] = []
            by_document[document_id].append((position, item))
        representatives = [
            max(
                by_document[document_id],
                key=lambda positioned: (
                    Retriever._authority_quality(query, candidates[positioned[1][0]]),
                    len(
                        str(
                            candidates[positioned[1][0]]["hit"].get("_source", {}).get("content")
                            or ""
                        ).strip()
                    )
                    >= 100,
                    -positioned[0],
                ),
            )[1]
            for document_id in document_order
        ]
        representative_indexes = {item[0] for item in representatives}
        deferred = [item for item in deduplicated if item[0] not in representative_indexes]
        return [*representatives, *deferred][:top_k]

    @staticmethod
    def _to_evidence(source: dict[str, Any], score: float) -> Evidence:
        """把搜索后端记录转换成业务层稳定的证据对象。"""

        return Evidence(
            chunk_id=source["chunk_id"],
            document_id=source["document_id"],
            version_id=source["version_id"],
            project_id=source["project_id"],
            filename=source["filename"],
            document_status=_source_status(source),
            document_type=source["document_type"],
            content=source["content"],
            heading_path=source.get("title_path") or source.get("heading_path"),
            section_id=source.get("section_id"),
            section_level=source.get("section_level"),
            breadcrumb=tuple(
                part.strip()
                for part in str(
                    source.get("section_path") or source.get("title_path") or ""
                ).split(">")
                if part.strip()
            ),
            location_confidence=(1.0 if int(source.get("section_level") or 0) > 0 else 0.7),
            page_number=source.get("page_number"),
            sheet_name=source.get("sheet_name"),
            cell_range=source.get("cell_range"),
            version_label=source.get("version_label"),
            score=score,
        )

    @staticmethod
    def _source_text(row: dict[str, Any]) -> str:
        """拼接可用于规则判断的来源文本，并统一为大小写不敏感形式。"""

        source = row["hit"].get("_source", {})
        return " ".join(
            str(source.get(field) or "")
            for field in ("filename", "title_path", "heading_path", "content")
        ).casefold()

    @staticmethod
    def _rerank_passage(row: dict[str, Any]) -> str:
        """为重排模型序列化正文及必要的来源、版本和状态上下文。"""

        source = row["hit"]["_source"]
        return "\n".join(
            [
                f"file={source.get('filename', '')}",
                f"status={_source_status(source)}",
                f"version={source.get('version_label', '')}",
                f"heading={source.get('title_path') or source.get('heading_path', '')}",
                str(source.get("content") or ""),
            ]
        )

    def _select_rrf_evidence(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        related_hits: list[dict[str, Any]],
        linked_identifiers: list[str] | None = None,
    ) -> list[Evidence]:
        """在禁用或无法使用 Rerank 时，按 RRF 顺序完成邻居与来源桥接。"""

        selected_rows = self._attach_short_chunk_neighbors(
            candidates[: self.settings.evidence_top_k],
            related_hits,
            max_extra=max(1, self.settings.evidence_top_k // 2),
        )
        selected_rows = self._attach_provenance_bridge_chunks(
            query,
            selected_rows,
            related_hits,
            linked_identifiers or [],
            max_extra=2,
        )
        return [
            self._to_evidence(row["hit"]["_source"], float(row.get("score") or 0.0))
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
        passages = [self._rerank_passage(row) for row in candidates]
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
        ranked = self._ensure_signal_coverage(
            query,
            candidates,
            ranked,
            rerank_top_n,
            additional_signals=semantic_anchors,
        )
        ranked = self._diversify_documents(
            candidates, ranked, self.settings.evidence_top_k, query=query
        )
        selected_rows = [
            {**candidates[index], "score": score}
            for index, score in ranked[: self.settings.evidence_top_k]
            if 0 <= index < len(candidates)
        ]
        selected_rows = self._attach_short_chunk_neighbors(
            selected_rows,
            related_hits,
            max_extra=max(1, self.settings.evidence_top_k // 2),
        )
        selected_rows = self._attach_provenance_bridge_chunks(
            query,
            selected_rows,
            related_hits,
            linked_identifiers,
            max_extra=2,
        )
        return [
            self._to_evidence(row["hit"]["_source"], float(row.get("score") or 0.0))
            for row in selected_rows
        ]

    @classmethod
    def _ensure_signal_coverage(
        cls,
        query: str,
        candidates: list[dict[str, Any]],
        ranked: list[tuple[int, float]],
        top_k: int,
        additional_signals: tuple[str, ...] = (),
    ) -> list[tuple[int, float]]:
        """在重排结果中补回关键字信号覆盖，避免模型漏掉精确条件。"""

        selected = [(index, score) for index, score in ranked if 0 <= index < len(candidates)]
        selected = list(dict.fromkeys(selected))
        mandatory_indexes: list[int] = []
        signals = tuple(dict.fromkeys((*_lexical_signals(query), *additional_signals)))
        for signal in (value.casefold() for value in signals):
            if any(signal in cls._source_text(candidates[index]) for index in mandatory_indexes):
                continue
            mandatory = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if signal in cls._source_text(candidate)
                ),
                None,
            )
            if mandatory is not None:
                mandatory_indexes.append(mandatory)
        mandatory_rows = [
            (index, candidates[index]["score"]) for index in mandatory_indexes[:top_k]
        ]
        remaining = []
        seen = set(mandatory_indexes)
        for item in selected:
            if item[0] not in seen:
                seen.add(item[0])
                remaining.append(item)
        return [*mandatory_rows, *remaining[: max(0, top_k - len(mandatory_rows))]]

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
            query, project_ids, statuses, self.settings.retrieval_top_k, principal_ids,
            **scope_kwargs,
        )
        vector_hits: list[dict[str, Any]] = []
        try:
            vector = self._query_embedding(query)
            vector_hits = self.index.vector_search(
                vector, project_ids, statuses, self.settings.retrieval_top_k, principal_ids,
                **scope_kwargs,
            )
        except Exception as exc:
            if not self.settings.allow_bm25_only:
                raise
            logger.warning(
                "Vector retrieval unavailable; BM25-only fallback: %s", type(exc).__name__
            )
        fused = self._rrf(lexical, vector_hits)
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
                normalized, project_ids, statuses, max(4, self.settings.evidence_top_k),
                principal_ids, **scope_kwargs,
            )
            if hits:
                channels.append(hits)
            if len(channels) >= 10:
                break
        return self._rrf(*channels) if channels else []

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
        baseline = self._search_once(query, project_ids, principal_ids, None)
        planned = self._search_once(query, project_ids, principal_ids, query_plan)
        result = self._merge_evidence_channels(
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

        if not getattr(self.settings, "hierarchical_retrieval_enabled", True):
            return RetrievalOutcome(
                evidence=self.search(query, project_ids, principal_ids, query_plan)
            )
        hint = document_hint or (query_plan.document_hint if query_plan else None)
        resolved: dict[str, Any] | None = None
        if document_id:
            resolved = self.index.document_by_id(document_id, project_ids, principal_ids)
            if resolved is None:
                logger.info("文档范围解析失败 reason=document_not_found document_id=%s", document_id)
                return RetrievalOutcome(
                    retrieval_intent="document_lookup",
                    resolved_scope="document_not_found",
                    retrieval_confidence=0.0,
                    failure_reason="document_not_found",
                )
        elif hint:
            candidates = self.index.document_candidates(
                hint, project_ids, principal_ids, limit=8
            )
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
                if query_plan
                and query_plan.retrieval_intent in {"global_lookup", "cross_source"}
                else "global_lookup"
            )
            return RetrievalOutcome(
                evidence=self.search(query, project_ids, principal_ids, query_plan),
                retrieval_intent=global_intent,
            )

        scoped_query = (
            query_plan.document_question
            if query_plan and query_plan.document_question
            else query
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
        focus_terms = list(query_plan.subjects) if query_plan else _query_subject_signals(query)
        focus_query = " ".join(focus_terms)
        retrieval_queries = (
            list(query_plan.retrieval_queries)
            if query_plan
            else ([focus_query] if focus_query else [])
        )
        for retrieval_query in retrieval_queries[:4]:
            retrieval_query = normalize_query(retrieval_query)
            if not retrieval_query or retrieval_query.casefold() == query.casefold():
                continue
            focused = self._retrieve_for_statuses(
                retrieval_query, project_ids, ["approved"], principal_ids, document_ids
            )
            fused = self._merge_fused(fused, focused)
        if section_hints:
            fused = self._boost_section_matches(fused, section_hints)
        semantic_anchors = query_plan.anchor_signals if query_plan else ()
        fused = self._boost_anchor_matches(fused, semantic_anchors)
        if (
            query_plan
            and query_plan.subjects
            and document_ids is None
            and not self._has_grounded_subject(fused, query_plan.subject_anchor_signals)
        ):
            logger.info("Semantic subject was not grounded in any candidate; abstaining")
            return []
        linked_identifiers = self._discover_linked_identifiers(
            query,
            fused,
            anchor_signals=semantic_anchors or None,
        )
        semantic_context = query_plan.rerank_context() if query_plan else ""
        if linked_identifiers:
            exact_expansion = self._retrieve_linked_identifier_rows(
                linked_identifiers,
                project_ids,
                ["approved"],
                principal_ids,
                document_ids,
            )
            fused = self._merge_fused(fused, exact_expansion)
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
            fused = self._merge_fused(fused, expanded)
        fused = self._deduplicate_content(fused)
        fused = self._enforce_exact_identifiers(query, fused, linked_identifiers)
        fused = self._prefer_complete_entity_matches(query, fused, linked_identifiers)
        related_document_ids = list(
            dict.fromkeys(
                str(row["hit"].get("_source", {}).get("document_id") or "")
                for row in fused
                if row["hit"].get("_source", {}).get("document_id")
            )
        )[: self.settings.evidence_top_k * 2]
        related_hits: list[dict[str, Any]] = []
        if related_document_ids:
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
        candidates = self._select_rerank_candidate_pool(
            fused,
            related_hits,
            limit=self.settings.rerank_candidates,
            expansion_slots=self.settings.evidence_top_k,
            query=query,
        )
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

    @staticmethod
    def _boost_section_matches(
        rows: list[dict[str, Any]], section_hints: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """把用户明确提及的标题片段作为软加权，不把它变成硬过滤。"""

        hints = [normalize_query(value).casefold() for value in section_hints if value.strip()]
        boosted = []
        for row in rows:
            source = row.get("hit", {}).get("_source", {})
            path = str(source.get("section_path") or source.get("title_path") or "").casefold()
            matches = sum(hint in path for hint in hints)
            boosted.append(
                {**row, "score": float(row.get("score") or 0.0) * (1 + 0.2 * matches)}
            )
        return sorted(boosted, key=lambda item: item["score"], reverse=True)
