"""Hybrid retrieval policy built on search and Qwen adapter ports."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.integrations.qwen import QwenAPIError, QwenClient
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

class Retriever:
    """Orchestrate deterministic hybrid recall before optional Qwen reranking."""

    def __init__(self, settings: Settings, index: SearchIndex, qwen: QwenClient):
        self.settings = settings
        self.index = index
        self.qwen = qwen
        self._query_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()

    def _query_embedding(self, query: str) -> list[float]:
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
        embeddings, _ = self.qwen.embeddings([normalized])
        if len(embeddings) != 1:
            raise ValueError("Query embedding response count does not match input")
        if len(self._query_cache) >= self.settings.query_embedding_cache_size:
            self._query_cache.popitem(last=False)
        self._query_cache[key] = (now, embeddings[0])
        return embeddings[0]

    @staticmethod
    def _rrf(*ranked_lists: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
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
        """Extract approved reference IDs and controlled aliases from subject anchors."""

        query_identifiers = {
            match.casefold()
            for pattern in (EXACT_IDENTIFIER, ACRONYM)
            for match in pattern.findall(normalize_query(query))
        }
        signals = [
            value.casefold()
            for value in (anchor_signals or tuple(_lexical_signals(query)))
        ]
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
                str(source.get(field) or "")
                for field in ("title_path", "heading_path", "content")
            )
            for value in CONTROLLED_ALIAS.findall(alias_text):
                normalized = value.casefold().strip()
                if normalized and normalized not in query_identifiers and normalized not in {
                    item.casefold() for item in discovered
                }:
                    discovered.append(value.strip())
                    if len(discovered) >= limit:
                        return discovered
        return discovered

    @staticmethod
    def _merge_fused(
        primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
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
        """Preserve baseline recall while allowing semantic planning to add evidence."""

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
        signals = [value.casefold() for value in subject_signals if value.strip()]
        return bool(signals) and any(
            signal in cls._source_text(row) for row in rows for signal in signals
        )

    @staticmethod
    def _deduplicate_content(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        """Reserve rerank slots for adjacent chunks from proven documents."""

        if limit <= 0:
            return []
        reserve = min(max(0, expansion_slots), max(0, limit - 1))
        base_count = max(1, limit - reserve)
        base = fused[:base_count]
        seen = {
            str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "")
            for row in base
        }
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
                        abs(
                            int(row["hit"].get("_source", {}).get("chunk_ordinal") or 0)
                            - ordinal
                        )
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
            if str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "")
            not in seen
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
        """Attach a nearby value block when a selected chunk is only a heading."""

        if max_extra <= 0:
            return selected
        seen = {
            str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "")
            for row in selected
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
        """Keep subject-to-reference mappings alongside downstream facts.

        Rerankers naturally favor chunks containing the final value.  In a
        multi-document join that can drop the requirement/registry chunk that
        proves why the downstream policy, matrix, or route belongs to the
        subject in the question.  This deterministic pass retains that
        provenance without inventing a relationship.
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
            str(row.get("hit", {}).get("_source", {}).get("chunk_id") or "")
            for row in selected
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
        """Score current approved evidence above history inside the same file."""

        normalized_query = normalize_query(query).casefold()
        text = Retriever._source_text(row)
        authoritative = (
            "approved", "current", "effective date", "signed", "authoritative",
            "正式", "当前", "生效", "已批准",
        )
        historical = (
            "not the approval page", "not current", "not the final decision", "retired",
            "historical", "superseded", "deprecated", "draft", "candidate",
            "never approved", "旧值", "退役", "历史", "草稿", "候选", "作废",
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
        """Put one authoritative, useful chunk per document before duplicates."""

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
                            candidates[positioned[1][0]]["hit"]
                            .get("_source", {})
                            .get("content")
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
            page_number=source.get("page_number"),
            sheet_name=source.get("sheet_name"),
            cell_range=source.get("cell_range"),
            version_label=source.get("version_label"),
            score=score,
        )

    @staticmethod
    def _source_text(row: dict[str, Any]) -> str:
        source = row["hit"].get("_source", {})
        return " ".join(
            str(source.get(field) or "")
            for field in ("filename", "title_path", "heading_path", "content")
        ).casefold()

    @staticmethod
    def _rerank_passage(row: dict[str, Any]) -> str:
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

    @classmethod
    def _ensure_signal_coverage(
        cls,
        query: str,
        candidates: list[dict[str, Any]],
        ranked: list[tuple[int, float]],
        top_k: int,
        additional_signals: tuple[str, ...] = (),
    ) -> list[tuple[int, float]]:
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
    ) -> list[dict[str, Any]]:
        lexical = self.index.lexical_search(
            query,
            project_ids,
            statuses,
            self.settings.retrieval_top_k,
            principal_ids,
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
            )
        except Exception as exc:
            if not self.settings.allow_bm25_only:
                raise
            logger.warning("Vector retrieval unavailable; BM25-only fallback: %s", type(exc).__name__)
        return self._rrf(lexical, vector_hits)

    def _retrieve_linked_identifier_rows(
        self,
        identifiers: list[str],
        project_ids: list[str],
        statuses: list[str],
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Expand approved cross-document IDs independently using exact lexical search.

        A single long semantic query can bury opaque English-only downstream
        documents.  Exact IDs are company data already grounded in retrieved
        evidence, so each can be expanded deterministically without another
        model or embedding call.
        """

        channels: list[list[dict[str, Any]]] = []
        seen: set[str] = set()
        for identifier in identifiers:
            normalized = identifier.upper()
            if normalized.casefold() in seen or not EXACT_IDENTIFIER.fullmatch(normalized):
                continue
            seen.add(normalized.casefold())
            hits = self.index.lexical_search(
                normalized,
                project_ids,
                statuses,
                max(4, self.settings.evidence_top_k),
                principal_ids,
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
        if query_plan is None:
            return self._search_once(query, project_ids, principal_ids, None)
        baseline = self._search_once(query, project_ids, principal_ids, None)
        planned = self._search_once(query, project_ids, principal_ids, query_plan)
        return self._merge_evidence_channels(
            baseline,
            planned,
            limit=max(self.settings.evidence_top_k * 2, self.settings.evidence_top_k + 4),
        )

    def _search_once(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None,
        query_plan: QueryPlan | None,
    ) -> list[Evidence]:
        query = normalize_query(query)
        fused = self._retrieve_for_statuses(query, project_ids, ["approved"], principal_ids)
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
                retrieval_query, project_ids, ["approved"], principal_ids
            )
            fused = self._merge_fused(fused, focused)
        semantic_anchors = query_plan.anchor_signals if query_plan else ()
        fused = self._boost_anchor_matches(fused, semantic_anchors)
        if (
            query_plan
            and query_plan.subjects
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
                expanded_query, project_ids, ["approved"], principal_ids
            )
            fused = self._merge_fused(fused, expanded)
        if len(fused) < 3:
            fallback = self._retrieve_for_statuses(
                query, project_ids, ["approved", "draft"], principal_ids
            )
            by_id = {row["hit"]["_source"]["chunk_id"]: row for row in fused}
            for row in fallback:
                by_id.setdefault(row["hit"]["_source"]["chunk_id"], row)
            fused = sorted(by_id.values(), key=lambda row: row["score"], reverse=True)
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
        passages = [self._rerank_passage(row) for row in candidates]
        try:
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
            ranked = self.qwen.rerank(rerank_query, passages, rerank_top_n)
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
        except QwenAPIError as exc:
            logger.warning("Rerank unavailable; using RRF ordering: %s", exc.code)
            selected_rows = self._attach_short_chunk_neighbors(
                candidates[: self.settings.evidence_top_k],
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
                self._to_evidence(row["hit"]["_source"], row["score"])
                for row in selected_rows
            ]
