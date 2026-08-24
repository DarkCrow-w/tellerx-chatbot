from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import ApiError, NotFoundError, TransportError

from app.config import Settings
from app.qwen import QwenAPIError, QwenClient

if TYPE_CHECKING:
    from app.query_understanding import QueryPlan

logger = logging.getLogger(__name__)

EXACT_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ACRONYM = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12}\d{2,})(?![A-Za-z0-9])")
ZH_ENTITY = re.compile(r"([\u3400-\u9fff]{2,20})(?:业务|系统|项目|模块)")
ZH_QUERY_SUBJECT = re.compile(
    r"^([\u3400-\u9fff]{2,20}?)\s*(?:当前|的|在|受|使用|由|如果|若|最新|发生|执行|需要)"
)
EN_ENTITY = re.compile(
    r"\b(?i:for|about)\s+([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})"
    r"(?=\s*[,\.?（(]|\s*$)",
)
EN_RELATED_SUBJECT = re.compile(
    r"\b(?i:knowledge|information|documents?|details?|content)\s+"
    r"(?:(?i:is)\s+)?(?i:related|relevant)\s+(?i:to)\s+"
    r"([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})(?=\s*[,\.?]|\s*$)"
)
CONTROLLED_ALIAS = re.compile(
    r"(?im)^(?:Chinese business name\s*/\s*中文业务称谓|"
    r"English operational name\s*/\s*英文运行称谓)\s*:\s*([^\r\n]+?)\s*$"
)
ZH_QUOTED_SUBJECT = re.compile(
    r"[\"“”「」『』]([\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9·_.\-/ ]{1,39}?)[\"“”「」『』]"
)
ZH_FOCUS_SPAN = r"([\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9·_.\-/ ]{1,39}?)"
ZH_RELATED_SUBJECT = re.compile(
    rf"^(?:与|和|跟|关于|有关|有关于|针对|围绕)\s*{ZH_FOCUS_SPAN}\s*"
    r"(?:相关|有关|关联|方面)(?:的)?"
)
ZH_ABOUT_SUBJECT = re.compile(
    rf"^(?:关于|有关|有关于|对于|针对|围绕)\s*{ZH_FOCUS_SPAN}"
    r"(?=\s*(?:，|,|的|都有哪些|有哪些|是什么|$))"
)
ZH_TOPIC_FIRST = re.compile(
    rf"^{ZH_FOCUS_SPAN}\s*[，,]\s*(?:请)?(?:介绍|说明|总结|概括|整理|列出|讲讲|说说)"
)
ZH_KNOWLEDGE_SUBJECT = re.compile(
    rf"^{ZH_FOCUS_SPAN}\s*(?:相关|有关|关联|方面)?(?:的)?(?:具体)?"
    r"(?:知识|信息|内容|资料|文档|规则|情况|详情|介绍|说明)"
)
ZH_DEFINITION_SUBJECT = re.compile(
    rf"^{ZH_FOCUS_SPAN}\s*(?:是什么|有哪些|都有哪些|是做什么的|怎么理解|什么意思|"
    r"如何定义|怎么样|有什么作用|如何使用)"
)
ZH_BARE_BLOCKERS = (
    "当前",
    "最新",
    "发生",
    "执行",
    "需要",
    "使用",
    "如果",
    "怎么办",
    "如何",
    "为什么",
    "什么",
    "多少",
    "哪个",
    "比较",
    "区别",
    "是否",
    "门槛",
    "接口",
    "审批",
    "超时",
)
ZH_DEFINITION_TAIL = re.compile(
    r"(?:的)?(?:当前|最新|正式)?(?:中文控制规则|控制规则|治理责任人|业务负责人|审批角色|"
    r"审批阈值|失败队列|接口路径|策略|规则|门槛|阈值|接口|状态|责任人|负责人|超时)$"
)
ZH_POLITE_PREFIXES = (
    re.compile(
        r"^(?:请问|请|麻烦(?:你)?|烦请|劳驾|能否|可否|你能(?:否)?|可以(?:请)?|是否可以)\s*"
    ),
    re.compile(
        r"^(?:(?:你)?帮我|为我|给我|我想(?:要|知道|了解)?(?:一下|下)?|"
        r"想(?:要|知道|了解)?(?:一下|下)?)\s*"
    ),
    re.compile(r"^(?:从)?(?:这个)?知识库(?:里|中)?\s*"),
    re.compile(
        r"^(?:列出|整理|汇总|查找|查询|查|搜索|介绍|说明|总结|概括|了解|知道|告诉我|"
        r"讲讲|说说|看看)"
        r"(?:一下|下)?\s*"
    ),
)


def normalize_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _strip_polite_prefixes(query: str) -> str:
    """Remove request scaffolding while preserving the user's business phrase."""
    value = query.strip(" \t\r\n，,。.!！?？；;：:")
    changed = True
    while changed and value:
        changed = False
        for pattern in ZH_POLITE_PREFIXES:
            stripped = pattern.sub("", value, count=1).strip()
            if stripped != value:
                value = stripped
                changed = True
    return value


def _query_subject_signals(query: str) -> list[str]:
    """Extract high-confidence subjects from common Chinese and English phrasing."""
    normalized = normalize_query(query)
    cleaned = _strip_polite_prefixes(normalized)
    values = [match.group(1) for match in ZH_QUOTED_SUBJECT.finditer(normalized)]
    structured_match = False
    for pattern in (
        ZH_RELATED_SUBJECT,
        ZH_ABOUT_SUBJECT,
        ZH_TOPIC_FIRST,
        ZH_DEFINITION_SUBJECT,
        ZH_KNOWLEDGE_SUBJECT,
    ):
        match = pattern.search(cleaned)
        if match:
            subject = match.group(1)
            if pattern is ZH_DEFINITION_SUBJECT:
                subject = ZH_DEFINITION_TAIL.sub("", subject).strip()
            values.append(subject)
            structured_match = True
            break
    bare = cleaned.removesuffix("吗").removesuffix("呢").removesuffix("吧").strip()
    if (
        not structured_match
        and 2 <= len(bare) <= 20
        and not any(term in bare for term in ZH_BARE_BLOCKERS)
        and re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9·_.\-/ ]+", bare)
    ):
        values.append(bare)
        structured_match = True
    if not structured_match:
        for match in ZH_QUERY_SUBJECT.finditer(cleaned):
            values.append(match.group(1))
    values.extend(match.group(1) for match in EN_ENTITY.finditer(normalized))
    values.extend(match.group(1) for match in EN_RELATED_SUBJECT.finditer(normalized))

    cleaned_values: list[str] = []
    for value in values:
        subject = value.strip(" \t\r\n，,。.!！?？；;：:\"'“”「」『』")
        if EXACT_IDENTIFIER.search(subject) or ACRONYM.search(subject):
            continue
        for suffix in ("业务", "系统", "项目", "模块"):
            if subject.endswith(suffix) and len(subject) > len(suffix):
                subject = subject[: -len(suffix)]
                break
        if 2 <= len(subject) <= 40:
            cleaned_values.append(subject)
    return list(dict.fromkeys(cleaned_values))


def _lexical_signals(query: str) -> list[str]:
    """Extract stable business identifiers and names without an LLM rewrite."""
    query = normalize_query(query)
    values = [*EXACT_IDENTIFIER.findall(query), *ACRONYM.findall(query)]
    values.extend(match.group(1) for match in ZH_ENTITY.finditer(query))
    values.extend(_query_subject_signals(query))
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

    def document_chunks(
        self,
        document_ids: list[str],
        project_ids: list[str],
        statuses: list[str],
        top_k: int,
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Load complete chunks for documents already proven relevant."""
        if not document_ids:
            return []
        index = self.settings.elasticsearch_read_alias
        if not self.client.indices.exists(index=index):
            return []
        result = self.client.search(
            index=index,
            size=top_k,
            query={
                "bool": {
                    "filter": [
                        *self._filters(project_ids, statuses, principal_ids),
                        {"terms": {"document_id": list(dict.fromkeys(document_ids))}},
                    ]
                }
            },
            sort=[{"document_id": "asc"}, {"chunk_ordinal": "asc"}],
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
            row
            for row in rows
            if any(signal in cls._source_text(row) for signal in signals)
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
                    merged[chunk_id] = {**row, "score": float(row.get("score") or 0.0) * weight}
                else:
                    merged[chunk_id]["score"] += float(row.get("score") or 0.0) * weight
        return sorted(merged.values(), key=lambda row: row["score"], reverse=True)

    @classmethod
    def _boost_anchor_matches(
        cls,
        rows: list[dict[str, Any]],
        anchor_signals: tuple[str, ...],
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
        cls,
        rows: list[dict[str, Any]],
        subject_signals: tuple[str, ...],
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
        """Reserve rerank slots for adjacent chunks from proven documents.

        A document-level hit is not enough for answering when the heading and
        the actual value live in different chunks.  Keep the strongest global
        candidates, then add one nearest unseen chunk for each leading
        document before filling any unused slots from the original ranking.
        """
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
                            int(
                                row["hit"].get("_source", {}).get("chunk_ordinal")
                                or 0
                            )
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
        generic_signals = {
            "业务",
            "系统",
            "项目",
            "模块",
            "规则",
            "接口",
            "文档",
            "策略",
        }
        if signal in generic_signals:
            return rows
        linked = {value.casefold() for value in linked_identifiers or []}
        matched = [
            row
            for row in rows
            if signal in cls._source_text(row)
            or any(identifier in cls._source_text(row) for identifier in linked)
        ]
        # A concrete named subject that is absent from both the original hits
        # and any approved bridge is an unknown entity.  Returning semantically
        # similar neighbours here would let near-collision aliases leak into an
        # answer (for example Mistbridge Clearing vs. Mistbridge Archive).
        return matched

    @staticmethod
    def _authority_quality(query: str, row: dict[str, Any]) -> int:
        """Score current, approved evidence above history inside the same file."""
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
        """Put one authoritative chunk per document before duplicate chunks."""
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

    def search(
        self,
        query: str,
        project_ids: list[str],
        principal_ids: list[str] | None = None,
        query_plan: QueryPlan | None = None,
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
            and not self._has_grounded_subject(
                fused,
                query_plan.subject_anchor_signals,
            )
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
            expanded_query = "\n".join(
                part
                for part in [
                    query,
                    semantic_context,
                    "Approved cross-document references: "
                    + " ".join(linked_identifiers),
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
                    "Approved cross-document references: "
                    + " ".join(linked_identifiers)
                    if linked_identifiers
                    else "",
                ]
                if part
            )
            rerank_top_n = min(
                len(candidates), max(self.settings.evidence_top_k * 3, self.settings.evidence_top_k)
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
            return [
                self._to_evidence(row["hit"]["_source"], row["score"])
                for row in selected_rows
            ]
