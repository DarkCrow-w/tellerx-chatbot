from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import ApiError, NotFoundError, TransportError

from app.config import Settings
from app.qwen import QwenAPIError, QwenClient

logger = logging.getLogger(__name__)

EXACT_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ACRONYM = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12}\d{2,})(?![A-Za-z0-9])")
ZH_ENTITY = re.compile(r"([\u3400-\u9fff]{2,20})(?:业务|系统|项目|模块)")
EN_ENTITY = re.compile(
    r"\b(?i:for|about)\s+([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})(?=\s*[,?])",
)


def normalize_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _lexical_signals(query: str) -> list[str]:
    """Extract stable business identifiers and names without an LLM rewrite."""
    query = normalize_query(query)
    values = [*EXACT_IDENTIFIER.findall(query), *ACRONYM.findall(query)]
    values.extend(match.group(1) for match in ZH_ENTITY.finditer(query))
    values.extend(match.group(1) for match in EN_ENTITY.finditer(query))
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _source_status(source: dict[str, Any]) -> str:
    return str(source.get("lifecycle_status") or source.get("document_status") or "")


@dataclass(slots=True)
class Evidence:
    chunk_id: str
    document_id: str
    version_id: str
    project_id: str
    filename: str
    document_status: str
    document_type: str
    content: str
    heading_path: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    version_label: str | None = None
    score: float = 0.0


class SearchIndex:
    """Elasticsearch-backed BM25 and dense-vector index.

    PostgreSQL and object storage remain authoritative.  This class deliberately
    uses separate read/write aliases so a fully validated generation can be
    activated atomically without changing query code.
    """

    def __init__(self, settings: Settings, client: Elasticsearch | Any | None = None):
        self.settings = settings
        self.client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> Elasticsearch:
        options: dict[str, Any] = {
            "hosts": [settings.elasticsearch_url],
            "verify_certs": settings.elasticsearch_verify_certs,
        }
        password = settings.elasticsearch_password
        if settings.elasticsearch_username and password:
            options["basic_auth"] = (settings.elasticsearch_username, password)
        return Elasticsearch(**options)

    @property
    def mapping(self) -> dict[str, Any]:
        return {
            "settings": {
                "number_of_shards": self.settings.elasticsearch_number_of_shards,
                "number_of_replicas": self.settings.elasticsearch_number_of_replicas,
                "refresh_interval": "5s",
                "analysis": {
                    "normalizer": {
                        "kb_keyword": {
                            "type": "custom",
                            "filter": ["lowercase", "asciifolding"],
                        }
                    }
                },
            },
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "version_id": {"type": "keyword"},
                    "project_id": {"type": "keyword"},
                    "document_type": {"type": "keyword"},
                    "lifecycle_status": {"type": "keyword"},
                    "version_label": {"type": "keyword"},
                    "visibility": {"type": "keyword"},
                    "acl_principals": {"type": "keyword"},
                    "effective_from": {"type": "date"},
                    "effective_to": {"type": "date"},
                    "is_current": {"type": "boolean"},
                    "is_searchable": {"type": "boolean"},
                    "filename": {
                        "type": "text",
                        "analyzer": "cjk",
                        "fields": {"raw": {"type": "keyword"}},
                    },
                    "title_path": {
                        "type": "text",
                        "analyzer": "cjk",
                        "fields": {"raw": {"type": "keyword"}},
                    },
                    "exact_terms": {"type": "keyword", "normalizer": "kb_keyword"},
                    "page_number": {"type": "integer"},
                    "sheet_name": {"type": "keyword"},
                    "cell_range": {"type": "keyword"},
                    "chunk_ordinal": {"type": "integer"},
                    "parent_chunk_id": {"type": "keyword"},
                    "previous_chunk_id": {"type": "keyword"},
                    "next_chunk_id": {"type": "keyword"},
                    "content_hash": {"type": "keyword"},
                    "record_hash": {"type": "keyword"},
                    "schema_version": {"type": "keyword"},
                    "embedding_fingerprint": {"type": "keyword"},
                    "content": {
                        "type": "text",
                        "analyzer": "cjk",
                        "fields": {"standard": {"type": "text", "analyzer": "standard"}},
                    },
                    "embedding": {
                        "type": "dense_vector",
                        "dims": self.settings.qwen_embedding_dimensions,
                        "index": True,
                        "similarity": "cosine",
                    },
                },
            },
        }

    def _alias_indexes(self, alias: str) -> list[str]:
        try:
            aliases = self.client.indices.get_alias(name=alias)
        except NotFoundError:
            return []
        aliases = getattr(aliases, "body", aliases)
        if not isinstance(aliases, Mapping):
            return []
        return [
            index
            for index, payload in aliases.items()
            if isinstance(payload, Mapping)
            and isinstance(payload.get("aliases"), Mapping)
        ]

    def current_write_index(self) -> str:
        indexes = self._alias_indexes(self.settings.elasticsearch_write_alias)
        return indexes[0] if indexes else self.settings.search_index_name

    def current_read_index(self) -> str:
        indexes = self._alias_indexes(self.settings.elasticsearch_read_alias)
        return indexes[0] if indexes else self.settings.search_index_name

    def trace_index_name(self) -> str:
        try:
            return self.current_read_index()
        except (ApiError, TransportError):
            return self.settings.elasticsearch_read_alias

    def ensure_index(self) -> None:
        index = self.settings.search_index_name
        self.create_index(index)

        actions: list[dict[str, Any]] = []
        if not self._alias_indexes(self.settings.elasticsearch_read_alias):
            actions.append(
                {
                    "add": {
                        "index": index,
                        "alias": self.settings.elasticsearch_read_alias,
                    }
                }
            )
        if not self._alias_indexes(self.settings.elasticsearch_write_alias):
            actions.append(
                {
                    "add": {
                        "index": index,
                        "alias": self.settings.elasticsearch_write_alias,
                        "is_write_index": True,
                    }
                }
            )
        if actions:
            self.client.indices.update_aliases(actions=actions)

    def create_index(self, index: str) -> None:
        if not self.client.indices.exists(index=index):
            self.client.indices.create(index=index, **self.mapping)

    def activate_alias(self, target_index: str | None = None) -> None:
        """Atomically switch both aliases to a fully verified physical index."""
        target = target_index or self.settings.search_index_name
        if not self.client.indices.exists(index=target):
            raise ValueError(f"Cannot activate missing Elasticsearch index: {target}")
        actions: list[dict[str, Any]] = []
        for alias in (
            self.settings.elasticsearch_read_alias,
            self.settings.elasticsearch_write_alias,
        ):
            for old_index in self._alias_indexes(alias):
                if old_index != target:
                    actions.append({"remove": {"index": old_index, "alias": alias}})
            if target not in self._alias_indexes(alias):
                add: dict[str, Any] = {"index": target, "alias": alias}
                if alias == self.settings.elasticsearch_write_alias:
                    add["is_write_index"] = True
                actions.append({"add": add})
        if actions:
            self.client.indices.update_aliases(actions=actions)

    def _normalize_document(self, document: dict[str, Any]) -> dict[str, Any]:
        source = dict(document)
        status = str(source.pop("document_status", source.get("lifecycle_status", "")))
        source["lifecycle_status"] = status
        source["title_path"] = source.pop("heading_path", source.get("title_path"))
        supplied_terms = source.pop("identifiers", source.get("exact_terms", [])) or []
        searchable = " ".join(
            str(source.get(field) or "") for field in ("filename", "title_path", "content")
        )
        extracted = [
            *EXACT_IDENTIFIER.findall(searchable),
            *ACRONYM.findall(searchable),
        ]
        source["exact_terms"] = sorted(
            {str(value).casefold() for value in [*supplied_terms, *extracted] if value}
        )
        source.setdefault("visibility", "public")
        source.setdefault("acl_principals", [])
        source.setdefault("is_current", status == "approved")
        source.setdefault("is_searchable", status != "deprecated")
        source.setdefault("chunk_ordinal", 0)
        source.setdefault("schema_version", str(self.settings.elasticsearch_schema_version))
        source.setdefault("embedding_fingerprint", self.settings.embedding_fingerprint)
        return source

    def index_chunks(
        self,
        documents: list[dict[str, Any]],
        *,
        target_index: str | None = None,
    ) -> None:
        if not documents:
            return
        target = target_index or self.settings.elasticsearch_write_alias
        if target_index:
            self.create_index(target)
        else:
            self.ensure_index()
        actions = [
            {
                "_op_type": "index",
                "_index": target,
                "_id": document["chunk_id"],
                "_source": self._normalize_document(document),
            }
            for document in documents
        ]
        helpers.bulk(
            self.client,
            actions,
            request_timeout=120,
            refresh="wait_for",
            raise_on_error=True,
            raise_on_exception=True,
        )

    def delete_version(self, version_id: str) -> None:
        target = self.settings.elasticsearch_write_alias
        if self.client.indices.exists(index=target):
            self.client.delete_by_query(
                index=target,
                query={"term": {"version_id": version_id}},
                conflicts="proceed",
                refresh=True,
            )

    def delete_stale_version_chunks(self, version_id: str, current_chunk_ids: list[str]) -> None:
        target = self.settings.elasticsearch_write_alias
        if not self.client.indices.exists(index=target):
            return
        query: dict[str, Any] = {"term": {"version_id": version_id}}
        if current_chunk_ids:
            query = {
                "bool": {
                    "filter": [{"term": {"version_id": version_id}}],
                    "must_not": [{"ids": {"values": current_chunk_ids}}],
                }
            }
        self.client.delete_by_query(
            index=target,
            query=query,
            conflicts="proceed",
            refresh=True,
        )

    @staticmethod
    def _filters(
        project_ids: list[str],
        statuses: list[str],
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        status_options: list[dict[str, Any]] = []
        if "approved" in statuses:
            status_options.append(
                {
                    "bool": {
                        "filter": [
                            {"term": {"lifecycle_status": "approved"}},
                            {"term": {"is_current": True}},
                        ]
                    }
                }
            )
        status_options.extend(
            {"term": {"lifecycle_status": status}}
            for status in statuses
            if status != "approved"
        )
        filters: list[dict[str, Any]] = [
            {"bool": {"should": status_options, "minimum_should_match": 1}},
            {"term": {"is_searchable": True}},
        ]
        if project_ids:
            filters.append({"terms": {"project_id": project_ids}})
        if principal_ids:
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"visibility": "public"}},
                            {"terms": {"acl_principals": principal_ids}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        else:
            filters.append({"term": {"visibility": "public"}})
        return filters

    def lexical_search(
        self,
        query: str,
        project_ids: list[str],
        statuses: list[str],
        top_k: int,
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        index = self.settings.elasticsearch_read_alias
        if not self.client.indices.exists(index=index):
            return []
        query = normalize_query(query)
        exact_terms = {match.casefold() for match in EXACT_IDENTIFIER.findall(query)}
        exact_terms.update(match.casefold() for match in ACRONYM.findall(query))
        should: list[dict[str, Any]] = [
            {"term": {"exact_terms": {"value": term, "boost": 100.0}}}
            for term in exact_terms
        ]
        for signal in _lexical_signals(query):
            should.extend(
                [
                    {"match_phrase": {"filename": {"query": signal, "boost": 50.0}}},
                    {"match_phrase": {"content": {"query": signal, "boost": 35.0}}},
                    {"match_phrase": {"title_path": {"query": signal, "boost": 20.0}}},
                ]
            )
        result = self.client.search(
            index=index,
            size=top_k,
            query={
                "bool": {
                    "filter": self._filters(project_ids, statuses, principal_ids),
                    "must": {
                        "multi_match": {
                            "query": query,
                            "fields": [
                                "filename^4",
                                "title_path^3",
                                "content^2",
                                "content.standard^2.5",
                            ],
                            "type": "best_fields",
                        }
                    },
                    "should": should,
                }
            },
            source_excludes=["embedding"],
        )
        return result.get("hits", {}).get("hits", [])

    def vector_search(
        self,
        vector: list[float],
        project_ids: list[str],
        statuses: list[str],
        top_k: int,
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        index = self.settings.elasticsearch_read_alias
        if not self.client.indices.exists(index=index):
            return []
        knn: dict[str, Any] = {
            "field": "embedding",
            "query_vector": vector,
            "k": top_k,
            "num_candidates": max(top_k, self.settings.vector_num_candidates),
            "filter": {"bool": {"filter": self._filters(project_ids, statuses, principal_ids)}},
        }
        if self.settings.vector_min_similarity is not None:
            knn["similarity"] = self.settings.vector_min_similarity
        result = self.client.search(
            index=index,
            size=top_k,
            knn=knn,
            source_excludes=["embedding"],
        )
        return result.get("hits", {}).get("hits", [])

    def count_version(self, version_id: str, *, target_index: str | None = None) -> int:
        index = target_index or self.settings.elasticsearch_read_alias
        if not self.client.indices.exists(index=index):
            return 0
        result = self.client.count(index=index, query={"term": {"version_id": version_id}})
        return int(result.get("count", 0))

    def count_all(self, *, target_index: str | None = None) -> int:
        index = target_index or self.settings.elasticsearch_read_alias
        if not self.client.indices.exists(index=index):
            return 0
        return int(self.client.count(index=index).get("count", 0))

    def version_records(self, version_id: str) -> list[tuple[int, str, str]]:
        """Return the minimum immutable fields needed for manifest reconciliation."""
        index = self.settings.elasticsearch_read_alias
        if not self.client.indices.exists(index=index):
            return []
        records = []
        for hit in helpers.scan(
            self.client,
            index=index,
            query={
                "query": {"term": {"version_id": version_id}},
                "_source": ["chunk_id", "chunk_ordinal", "record_hash"],
            },
            request_timeout=120,
        ):
            source = hit.get("_source", {})
            records.append(
                (
                    int(source.get("chunk_ordinal", 0)),
                    str(source.get("chunk_id") or hit.get("_id") or ""),
                    str(source.get("record_hash") or ""),
                )
            )
        return sorted(records)

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except ApiError:
            return False

    def status(self) -> dict[str, Any]:
        try:
            health = self.client.cluster.health()
            read_indexes = self._alias_indexes(self.settings.elasticsearch_read_alias)
            write_indexes = self._alias_indexes(self.settings.elasticsearch_write_alias)
            return {
                "available": True,
                "cluster_status": health.get("status"),
                "read_alias": read_indexes,
                "write_alias": write_indexes,
                "physical_index": (
                    read_indexes[0] if read_indexes else self.settings.search_index_name
                ),
            }
        except (ApiError, TransportError) as exc:
            return {"available": False, "error_type": type(exc).__name__}


class Retriever:
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
