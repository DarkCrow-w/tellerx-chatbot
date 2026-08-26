from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings

logger = logging.getLogger(__name__)

EXACT_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ACRONYM = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12}\d{2,})(?![A-Za-z0-9])")
ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:/-]*", re.IGNORECASE)
CJK_SPAN = re.compile(r"[\u3400-\u9fff]+")
ZH_ENTITY = re.compile(r"([\u3400-\u9fff]{2,20})(?:业务|系统|项目|模块)")
ZH_QUERY_SUBJECT = re.compile(
    r"^([\u3400-\u9fff]{2,20}?)\s*(?:当前|的|在|受|使用|由|如果|若|最新|发生|执行|需要)"
)
EN_ENTITY = re.compile(
    r"\b(?i:for|about)\s+([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})"
    r"(?=\s*[,\.?（(]|\s*$)"
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
    re.compile(r"^(?:请问|请|麻烦(?:你)?|烦请|劳驾|能否|可否|你能(?:否)?|可以(?:请)?|是否可以)\s*"),
    re.compile(
        r"^(?:(?:你)?帮我|为我|给我|我想(?:要|知道|了解)?(?:一下|下)?|"
        r"想(?:要|知道|了解)?(?:一下|下)?)\s*"
    ),
    re.compile(r"^(?:从)?(?:这个)?知识库(?:里|中)?\s*"),
    re.compile(
        r"^(?:列出|整理|汇总|查找|查询|查|搜索|介绍|说明|总结|概括|了解|知道|告诉我|"
        r"讲讲|说说|看看)(?:一下|下)?\s*"
    ),
)


def normalize_query(value: str) -> str:
    """规范化用户输入，同时保留大小写敏感的实体信号。"""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def _strip_polite_prefixes(query: str) -> str:
    """移除中文请求套话，但保留真正的业务短语。"""

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


def _structured_subject(query: str) -> str | None:
    """按优先级提取带明确句式的中文业务主题。"""

    for pattern in (
        ZH_RELATED_SUBJECT,
        ZH_ABOUT_SUBJECT,
        ZH_TOPIC_FIRST,
        ZH_DEFINITION_SUBJECT,
        ZH_KNOWLEDGE_SUBJECT,
    ):
        match = pattern.search(query)
        if match is None:
            continue
        subject = match.group(1)
        if pattern is ZH_DEFINITION_SUBJECT:
            subject = ZH_DEFINITION_TAIL.sub("", subject).strip()
        return subject
    return None


def _bare_subject(query: str) -> str | None:
    """仅在短语足够短且不含疑问词时接受裸实体问法。"""

    bare = query.removesuffix("吗").removesuffix("呢").removesuffix("吧").strip()
    has_allowed_characters = re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9·_.\-/ ]+", bare)
    if (
        2 <= len(bare) <= 20
        and not any(term in bare for term in ZH_BARE_BLOCKERS)
        and has_allowed_characters
    ):
        return bare
    return None


def _clean_subject(value: str) -> str | None:
    """清理主题边界字符，并排除 ID、缩写和过长文本。"""

    subject = value.strip(" \t\r\n，,。.!！?？；;：:\"'“”「」『』")
    if EXACT_IDENTIFIER.search(subject) or ACRONYM.search(subject):
        return None
    for suffix in ("业务", "系统", "项目", "模块"):
        if subject.endswith(suffix) and len(subject) > len(suffix):
            subject = subject[: -len(suffix)]
            break
    return subject if 2 <= len(subject) <= 40 else None


def _query_subject_signals(query: str) -> list[str]:
    """从常见中英文问法中提取高置信业务主题。"""

    normalized = normalize_query(query)
    cleaned = _strip_polite_prefixes(normalized)
    values = [match.group(1) for match in ZH_QUOTED_SUBJECT.finditer(normalized)]
    subject = _structured_subject(cleaned) or _bare_subject(cleaned)
    if subject is not None:
        values.append(subject)
    else:
        values.extend(match.group(1) for match in ZH_QUERY_SUBJECT.finditer(cleaned))
    values.extend(match.group(1) for match in EN_ENTITY.finditer(normalized))
    values.extend(match.group(1) for match in EN_RELATED_SUBJECT.finditer(normalized))

    cleaned_values = [subject for value in values if (subject := _clean_subject(value))]
    return list(dict.fromkeys(cleaned_values))


def _lexical_signals(query: str) -> list[str]:
    """无需模型调用，确定性提取稳定标识和业务实体。"""

    normalized = normalize_query(query)
    values = [*EXACT_IDENTIFIER.findall(normalized), *ACRONYM.findall(normalized)]
    values.extend(match.group(1) for match in ZH_ENTITY.finditer(normalized))
    values.extend(_query_subject_signals(normalized))
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _source_status(source: dict[str, Any]) -> str:
    """兼容搜索投影与业务对象的状态字段名。"""

    return str(source.get("lifecycle_status") or source.get("document_status") or "")


def normalize_search_text(value: str) -> str:
    """生成适合索引比较的 NFKC、大小写不敏感文本。"""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def lexical_tokens(value: str, *, limit: int = 512) -> list[str]:
    """为 PostgreSQL 全文检索生成确定性的中英文混合 Token。

    PostgreSQL 内置解析器不会切分中文业务名，因此应用侧保存英文单词、重叠的
    中文二元组以及较短的完整中文短语，无需服务端分词扩展也能区分内部术语。
    """

    normalized = normalize_search_text(value)
    tokens: list[str] = []
    tokens.extend(ASCII_TOKEN.findall(normalized))
    for span in CJK_SPAN.findall(normalized):
        if 2 <= len(span) <= 24:
            tokens.append(span)
        if len(span) == 1:
            tokens.append(span)
        else:
            tokens.extend(span[index : index + 2] for index in range(len(span) - 1))
    return list(dict.fromkeys(token for token in tokens if token))[:limit]


def _vector_literal(vector: Iterable[float]) -> str:
    """将向量编码为 pgvector 可解析且精度稳定的文本字面量。"""

    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


class SearchIndex:
    """基于 PostgreSQL 全文检索与 pgvector 的搜索端口实现。"""

    table_name = "chunk_search_index"

    def __init__(self, settings: Settings, engine: Engine | Any | None = None):
        """初始化搜索表配置，并允许测试注入数据库引擎。"""

        self.settings = settings
        self.table_name = settings.postgres_search_table
        self.engine = engine or create_engine(settings.database_url, pool_pre_ping=True)

    def close(self) -> None:
        """释放搜索数据库连接池。"""

        self.engine.dispose()

    @staticmethod
    def _source_select() -> str:
        """返回各检索通道共用的来源字段投影。"""

        return """
            s.chunk_id,
            c.id AS source_chunk_id,
            d.id AS document_id,
            v.id AS version_id,
            d.project_id,
            d.filename,
            v.lifecycle_status,
            d.document_type,
            d.visibility,
            v.version_label,
            v.effective_at AS effective_from,
            v.effective_to,
            v.is_current,
            (v.technical_status = 'searchable' AND NOT d.is_deleted) AS is_searchable,
            c.heading_path AS title_path,
            c.page_number,
            c.sheet_name,
            c.cell_range,
            c.ordinal AS chunk_ordinal,
            c.parent_chunk_id,
            c.previous_chunk_id,
            c.next_chunk_id,
            c.content,
            c.content_hash,
            c.record_hash
        """

    @staticmethod
    def _joins() -> str:
        """返回搜索投影关联事实表的公共 JOIN 片段。"""

        return """
            FROM chunk_search_index s
            JOIN chunks c ON c.id = s.chunk_id
            JOIN document_versions v ON v.id = c.version_id
            JOIN documents d ON d.id = v.document_id
        """

    @staticmethod
    def _hit(row: Any, score: float | None = None) -> dict[str, Any]:
        """将 SQL 行转换成检索服务使用的统一命中结构。"""

        mapping = dict(row._mapping if hasattr(row, "_mapping") else row)
        mapping.pop("source_chunk_id", None)
        raw_score = float(score if score is not None else mapping.pop("score", 0.0) or 0.0)
        chunk_id = str(mapping.get("chunk_id") or "")
        return {"_id": chunk_id, "_score": raw_score, "_source": mapping}

    @staticmethod
    def _status_clause(statuses: list[str], params: dict[str, Any]) -> str:
        """构造版本生命周期过滤；approved 只允许当前版本。"""

        clauses: list[str] = []
        if "approved" in statuses:
            clauses.append("(v.lifecycle_status = 'approved' AND v.is_current IS TRUE)")
        others = list(dict.fromkeys(status for status in statuses if status != "approved"))
        if others:
            params["other_statuses"] = others
            clauses.append("v.lifecycle_status IN :other_statuses")
        return "(" + " OR ".join(clauses or ["FALSE"]) + ")"

    @classmethod
    def _scope_clause(
        cls,
        project_ids: list[str],
        statuses: list[str],
        principal_ids: list[str] | None,
        params: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """统一拼装项目、状态、可搜索性和 ACL 数据权限范围。"""

        clauses = [
            cls._status_clause(statuses, params),
            "v.technical_status = 'searchable'",
            "d.is_deleted IS FALSE",
        ]
        expanding: list[str] = []
        if "other_statuses" in params:
            expanding.append("other_statuses")
        if project_ids:
            params["project_ids"] = list(dict.fromkeys(project_ids))
            clauses.append("d.project_id IN :project_ids")
            expanding.append("project_ids")
        if principal_ids:
            params["principal_ids"] = list(dict.fromkeys(principal_ids))
            clauses.append(
                "(d.visibility = 'public' OR EXISTS ("
                "SELECT 1 FROM document_acl acl "
                "WHERE acl.document_id = d.id AND acl.permission = 'read' "
                "AND acl.principal_id IN :principal_ids))"
            )
            expanding.append("principal_ids")
        else:
            clauses.append("d.visibility = 'public'")
        return " AND ".join(clauses), expanding

    @staticmethod
    def _statement(sql: str, expanding: list[str] | None = None):
        """为 SQLAlchemy IN 参数绑定 expanding 语义。"""

        statement = text(sql)
        for name in expanding or []:
            statement = statement.bindparams(bindparam(name, expanding=True))
        return statement

    def current_write_index(self) -> str:
        """返回包含向量指纹的当前写入目标标识。"""

        return f"postgresql:{self.table_name}:{self.settings.embedding_fingerprint}"

    def current_read_index(self) -> str:
        """返回当前读取目标；PostgreSQL 实现与写入目标相同。"""

        return self.current_write_index()

    def trace_index_name(self) -> str:
        """返回写入查询追踪记录的搜索目标名称。"""

        return self.current_read_index()

    def ensure_index(self) -> None:
        """迁移表或必要 PostgreSQL 扩展缺失时让应用启动失败。"""

        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT to_regclass(:table_name) IS NOT NULL AS table_ready, "
                        "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') AS vector_ready, "
                        "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') AS trgm_ready"
                    ),
                    {"table_name": self.table_name},
                )
                .mappings()
                .one()
            )
        missing = [
            name
            for name, ready in (
                (self.table_name, row["table_ready"]),
                ("vector extension", row["vector_ready"]),
                ("pg_trgm extension", row["trgm_ready"]),
            )
            if not ready
        ]
        if missing:
            raise RuntimeError("PostgreSQL search schema is not ready: " + ", ".join(missing))

    def create_index(self, _: str) -> None:
        """兼容搜索端口的建索引入口；实际结构由数据库迁移创建。"""

        self.ensure_index()

    def activate_alias(self, _: str | None = None) -> None:
        """兼容别名切换端口；PostgreSQL 实现只需验证结构。"""

        # PostgreSQL 更新本身具备事务性，不存在外部搜索引擎的别名切换步骤。
        self.ensure_index()

    @staticmethod
    def _document_text(document: dict[str, Any]) -> tuple[str, str, list[str]]:
        """生成原文、带字段权重的词法文本及精确标识集合。"""

        filename = str(document.get("filename") or "")
        title = str(document.get("title_path") or document.get("heading_path") or "")
        content = str(document.get("content") or "")
        raw_text = normalize_search_text(f"{filename} {title} {content}")
        # lexical_tokens 会在单字段内去重，因此必须先分别分词再重复 Token，才能
        # 真正保留文件名和标题的权重。
        filename_tokens = lexical_tokens(filename)
        title_tokens = lexical_tokens(title)
        content_tokens = lexical_tokens(content)
        lexical_text = " ".join(filename_tokens * 4 + title_tokens * 3 + content_tokens)
        supplied = document.get("exact_terms") or document.get("identifiers") or []
        exact_terms = {
            str(value).casefold()
            for value in [
                *supplied,
                *EXACT_IDENTIFIER.findall(raw_text),
                *ACRONYM.findall(raw_text),
            ]
            if value
        }
        return raw_text, lexical_text, sorted(exact_terms)

    def index_chunks(
        self,
        documents: list[dict[str, Any]],
        *,
        target_index: str | None = None,
    ) -> None:
        """批量 UPSERT 分块搜索投影；无向量记录仍可参与词法检索。"""

        del target_index
        if not documents:
            return
        sql = text(
            """
            INSERT INTO chunk_search_index (
                chunk_id, embedding, embedding_fingerprint,
                raw_text, lexical_text, exact_terms, record_hash, updated_at
            ) VALUES (
                :chunk_id, CAST(:embedding AS vector), :embedding_fingerprint,
                :raw_text, :lexical_text, :exact_terms, :record_hash, now()
            )
            ON CONFLICT (chunk_id) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                embedding_fingerprint = EXCLUDED.embedding_fingerprint,
                raw_text = EXCLUDED.raw_text,
                lexical_text = EXCLUDED.lexical_text,
                exact_terms = EXCLUDED.exact_terms,
                record_hash = EXCLUDED.record_hash,
                updated_at = now()
            """
        )
        rows = []
        for document in documents:
            raw_text, lexical_text, exact_terms = self._document_text(document)
            embedding = document.get("embedding")
            rows.append(
                {
                    "chunk_id": document["chunk_id"],
                    "embedding": _vector_literal(embedding) if embedding is not None else None,
                    "embedding_fingerprint": (
                        self.settings.embedding_fingerprint if embedding is not None else None
                    ),
                    "raw_text": raw_text,
                    "lexical_text": lexical_text,
                    "exact_terms": exact_terms,
                    "record_hash": document.get("record_hash")
                    or hashlib.sha256(raw_text.encode()).hexdigest(),
                }
            )
        with self.engine.begin() as connection:
            connection.execute(sql, rows)

    def delete_version(self, version_id: str) -> None:
        """删除指定文档版本的全部搜索投影。"""

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM chunk_search_index s USING chunks c "
                    "WHERE s.chunk_id = c.id AND c.version_id = :version_id"
                ),
                {"version_id": version_id},
            )

    def clear(self) -> None:
        """清空全部搜索投影，供受控的离线重建使用。"""

        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM chunk_search_index"))

    def delete_stale_version_chunks(self, version_id: str, current_chunk_ids: list[str]) -> None:
        """删除重建后不再属于当前版本清单的陈旧分块投影。"""

        params: dict[str, Any] = {"version_id": version_id}
        sql = (
            "DELETE FROM chunk_search_index s USING chunks c "
            "WHERE s.chunk_id = c.id AND c.version_id = :version_id"
        )
        expanding: list[str] = []
        if current_chunk_ids:
            params["current_chunk_ids"] = current_chunk_ids
            sql += " AND s.chunk_id NOT IN :current_chunk_ids"
            expanding.append("current_chunk_ids")
        with self.engine.begin() as connection:
            connection.execute(self._statement(sql, expanding), params)

    def prune_ineligible(self) -> int:
        """移除来源版本已不再有效可搜索的孤立投影。"""

        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "DELETE FROM chunk_search_index s WHERE NOT EXISTS ("
                    "SELECT 1 FROM chunks c "
                    "JOIN document_versions v ON v.id = c.version_id "
                    "JOIN documents d ON d.id = v.document_id "
                    "WHERE c.id = s.chunk_id AND NOT d.is_deleted "
                    "AND v.technical_status = 'searchable' "
                    "AND (v.lifecycle_status = 'draft' OR "
                    "(v.lifecycle_status = 'approved' AND v.is_current IS TRUE)))"
                )
            )
        return int(result.rowcount or 0)

    def lexical_search(
        self,
        query: str,
        project_ids: list[str],
        statuses: list[str],
        top_k: int,
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """执行带精确标识提升、中文信号匹配和 ACL 过滤的词法检索。"""

        normalized = normalize_search_text(query)
        tokens = lexical_tokens(normalized, limit=64)
        if not tokens:
            return []
        params: dict[str, Any] = {
            # 使用 websearch_to_tsquery 是为了容忍含 -、/ 的业务标识；用户输入的
            # 非法 tsquery 语法不能让普通知识查询变成 500 错误。
            "ts_query": " OR ".join(f'"{token}"' for token in tokens),
            "raw_query": normalized,
            "top_k": top_k,
        }
        scope, expanding = self._scope_clause(project_ids, statuses, principal_ids, params)
        exact_terms = sorted(
            {
                *[value.casefold() for value in EXACT_IDENTIFIER.findall(normalized)],
                *[value.casefold() for value in ACRONYM.findall(normalized)],
            }
        )
        exact_score = "0.0"
        exact_match = "FALSE"
        if exact_terms:
            params["exact_terms"] = exact_terms
            exact_score = (
                "CASE WHEN s.exact_terms && CAST(:exact_terms AS text[]) THEN 100.0 ELSE 0.0 END"
            )
            exact_match = "s.exact_terms && CAST(:exact_terms AS text[])"
        signals = [
            signal
            for signal in [*exact_terms, *lexical_tokens(normalized, limit=12)]
            if len(signal) >= 2
        ][:12]
        signal_scores: list[str] = []
        signal_matches: list[str] = []
        for index, signal in enumerate(dict.fromkeys(signals)):
            key = f"signal_{index}"
            params[key] = signal
            signal_scores.append(
                f"CASE WHEN position(:{key} in s.raw_text) > 0 THEN 8.0 ELSE 0.0 END"
            )
            signal_matches.append(f"position(:{key} in s.raw_text) > 0")
        signal_score = " + ".join(signal_scores) or "0.0"
        candidate_match = " OR ".join(
            ["s.search_vector @@ websearch.query", exact_match, *signal_matches]
        )
        sql = f"""
            WITH websearch AS (
                SELECT websearch_to_tsquery('simple', :ts_query) AS query
            )
            SELECT {self._source_select()},
                   (ts_rank_cd(s.search_vector, websearch.query, 32) * 20.0
                    + {exact_score} + {signal_score}
                    + similarity(s.raw_text, :raw_query)) AS score
            {self._joins()}
            CROSS JOIN websearch
            WHERE {scope}
              AND ({candidate_match})
            ORDER BY score DESC, s.chunk_id
            LIMIT :top_k
        """
        with self.engine.connect() as connection:
            rows = connection.execute(self._statement(sql, expanding), params).fetchall()
        return [self._hit(row) for row in rows]

    def vector_search(
        self,
        vector: list[float],
        project_ids: list[str],
        statuses: list[str],
        top_k: int,
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """在同一数据权限范围内执行余弦相似度向量检索。"""

        params: dict[str, Any] = {
            "embedding": _vector_literal(vector),
            "embedding_fingerprint": self.settings.embedding_fingerprint,
            "top_k": top_k,
        }
        scope, expanding = self._scope_clause(project_ids, statuses, principal_ids, params)
        threshold = ""
        if self.settings.vector_min_similarity is not None:
            params["minimum_similarity"] = self.settings.vector_min_similarity
            threshold = (
                "AND (1.0 - (s.embedding <=> CAST(:embedding AS vector))) >= :minimum_similarity"
            )
        sql = f"""
            SELECT {self._source_select()},
                   (1.0 - (s.embedding <=> CAST(:embedding AS vector))) AS score
            {self._joins()}
            WHERE {scope}
              AND s.embedding IS NOT NULL
              AND s.embedding_fingerprint = :embedding_fingerprint
              {threshold}
            ORDER BY s.embedding <=> CAST(:embedding AS vector), s.chunk_id
            LIMIT :top_k
        """
        with self.engine.begin() as connection:
            self._configure_vector_scan(connection)
            rows = connection.execute(self._statement(sql, expanding), params).fetchall()
        return [self._hit(row) for row in rows]

    def _configure_vector_scan(self, connection: Connection) -> None:
        """为当前事务配置 HNSW 搜索深度和严格有序的迭代扫描。"""

        ef_search = max(40, int(self.settings.pgvector_hnsw_ef_search))
        connection.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
        connection.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))

    def document_chunks(
        self,
        document_ids: list[str],
        project_ids: list[str],
        statuses: list[str],
        top_k: int,
        principal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按文档批量取回有序分块，用于相邻内容和跨文档关系扩展。"""

        if not document_ids:
            return []
        params: dict[str, Any] = {
            "document_ids": list(dict.fromkeys(document_ids)),
            "top_k": top_k,
        }
        scope, expanding = self._scope_clause(project_ids, statuses, principal_ids, params)
        expanding.append("document_ids")
        sql = f"""
            SELECT {self._source_select()}, 0.0 AS score
            {self._joins()}
            WHERE {scope} AND d.id IN :document_ids
            ORDER BY d.id, c.ordinal
            LIMIT :top_k
        """
        with self.engine.connect() as connection:
            rows = connection.execute(self._statement(sql, expanding), params).fetchall()
        return [self._hit(row) for row in rows]

    def count_version(self, version_id: str, *, target_index: str | None = None) -> int:
        """统计指定文档版本已发布的分块数量。"""

        del target_index
        with self.engine.connect() as connection:
            return int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM chunk_search_index s "
                        "JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = :version_id"
                    ),
                    {"version_id": version_id},
                )
                or 0
            )

    def count_all(self, *, target_index: str | None = None) -> int:
        """统计当前搜索投影中的全部分块。"""

        del target_index
        with self.engine.connect() as connection:
            return int(connection.scalar(text("SELECT count(*) FROM chunk_search_index")) or 0)

    def version_records(self, version_id: str) -> list[tuple[int, str, str]]:
        """返回构建版本清单哈希所需的有序投影元组。"""

        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.ordinal, s.chunk_id, s.record_hash "
                    "FROM chunk_search_index s JOIN chunks c ON c.id = s.chunk_id "
                    "WHERE c.version_id = :version_id ORDER BY c.ordinal, s.chunk_id"
                ),
                {"version_id": version_id},
            ).fetchall()
        return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]

    def ping(self) -> bool:
        """执行轻量连通性检查，不向调用方泄露数据库异常。"""

        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def status(self) -> dict[str, Any]:
        """返回数据库、扩展、投影表和已索引数量的运维状态。"""

        try:
            with self.engine.connect() as connection:
                row = (
                    connection.execute(
                        text(
                            "SELECT current_setting('server_version') AS server_version, "
                            "(SELECT extversion FROM pg_extension WHERE extname = 'vector') "
                            "AS vector_version, "
                            "(SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm') "
                            "AS trgm_version, "
                            "to_regclass(:table_name) IS NOT NULL AS table_ready"
                        ),
                        {"table_name": self.table_name},
                    )
                    .mappings()
                    .one()
                )
                indexed_chunks = (
                    int(connection.scalar(text("SELECT count(*) FROM chunk_search_index")) or 0)
                    if row["table_ready"]
                    else 0
                )
            ready = bool(row["table_ready"] and row["vector_version"] and row["trgm_version"])
            return {
                "available": ready,
                "backend": "postgresql-pgvector-fts",
                "server_version": row["server_version"],
                "vector_version": row["vector_version"],
                "trgm_version": row["trgm_version"],
                "table_ready": bool(row["table_ready"]),
                "physical_index": self.current_read_index(),
                "indexed_chunks": indexed_chunks,
            }
        except SQLAlchemyError as exc:
            logger.warning("PostgreSQL search status failed: %s", type(exc).__name__)
            return {
                "available": False,
                "backend": "postgresql-pgvector-fts",
                "error_type": type(exc).__name__,
            }
