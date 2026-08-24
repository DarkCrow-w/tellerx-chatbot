"""Hybrid retrieval policy built on search and Qwen adapter ports."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

from app.core.config import Settings
from app.integrations.qwen import QwenAPIError, QwenClient
from app.integrations.search import (
    ACRONYM,
    EN_ENTITY,
    EXACT_IDENTIFIER,
    ZH_ENTITY,
    SearchIndex,
    _lexical_signals,
    _source_status,
    normalize_query,
)
from app.knowledge.evidence import Evidence

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
    def _enforce_exact_identifiers(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        identifiers = {
            match.casefold()
            for pattern in (EXACT_IDENTIFIER, ACRONYM)
            for match in pattern.findall(normalize_query(query))
        }
        if not identifiers:
            return rows
        matched: list[dict[str, Any]] = []
        covered: set[str] = set()
        for row in rows:
            searchable = Retriever._source_text(row)
            row_identifiers = {identifier for identifier in identifiers if identifier in searchable}
            if row_identifiers:
                matched.append(row)
                covered.update(row_identifiers)
        return matched if covered == identifiers else []

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
    def _prefer_complete_entity_matches(
        cls, query: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        normalized = normalize_query(query)
        entity_signals = [
            *[match.group(1) for match in ZH_ENTITY.finditer(normalized)],
            *[match.group(1) for match in EN_ENTITY.finditer(normalized)],
        ]
        if len(entity_signals) != 1:
            return rows
        signal = entity_signals[0].casefold()
        matched = [row for row in rows if signal in cls._source_text(row)]
        return matched or rows

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
    ) -> list[tuple[int, float]]:
        selected = [(index, score) for index, score in ranked if 0 <= index < len(candidates)]
        selected = list(dict.fromkeys(selected))
        mandatory_indexes: list[int] = []
        for signal in (value.casefold() for value in _lexical_signals(query)):
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

    def search(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None = None,
    ) -> list[Evidence]:
        query = normalize_query(query)
        fused = self._retrieve_for_statuses(query, project_ids, ["approved"], principal_ids)
        if len(fused) < 3:
            fallback = self._retrieve_for_statuses(
                query, project_ids, ["approved", "draft"], principal_ids
            )
            by_id = {row["hit"]["_source"]["chunk_id"]: row for row in fused}
            for row in fallback:
                by_id.setdefault(row["hit"]["_source"]["chunk_id"], row)
            fused = sorted(by_id.values(), key=lambda row: row["score"], reverse=True)
        fused = self._deduplicate_content(fused)
        fused = self._enforce_exact_identifiers(query, fused)
        fused = self._prefer_complete_entity_matches(query, fused)
        candidates = fused[: self.settings.rerank_candidates]
        if not candidates:
            return []
        passages = [self._rerank_passage(row) for row in candidates]
        try:
            ranked = self.qwen.rerank(query, passages, self.settings.evidence_top_k)
            ranked = self._ensure_signal_coverage(
                query, candidates, ranked, self.settings.evidence_top_k
            )
            return [
                self._to_evidence(candidates[index]["hit"]["_source"], score)
                for index, score in ranked[: self.settings.evidence_top_k]
                if 0 <= index < len(candidates)
            ]
        except QwenAPIError as exc:
            logger.warning("Rerank unavailable; using RRF ordering: %s", exc.code)
            return [
                self._to_evidence(row["hit"]["_source"], row["score"])
                for row in candidates[: self.settings.evidence_top_k]
            ]

