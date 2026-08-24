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
EN_ENTITY = re.compile(
    r"\b(?i:for|about)\s+([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})(?=\s*[,?])"
)


def normalize_query(value: str) -> str:
    """Normalize user input without destroying case-sensitive entity signals."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def _lexical_signals(query: str) -> list[str]:
    """Extract stable identifiers and business entities without another model call."""

    normalized = normalize_query(query)
    values = [*EXACT_IDENTIFIER.findall(normalized), *ACRONYM.findall(normalized)]
    values.extend(match.group(1) for match in ZH_ENTITY.finditer(normalized))
    values.extend(match.group(1) for match in EN_ENTITY.finditer(normalized))
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _source_status(source: dict[str, Any]) -> str:
    return str(source.get("lifecycle_status") or source.get("document_status") or "")


def normalize_search_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def lexical_tokens(value: str, *, limit: int = 512) -> list[str]:
    """Create deterministic mixed Chinese/English tokens for PostgreSQL FTS.

    PostgreSQL's built-in parser does not segment Chinese business names.  The
    application therefore stores ASCII words and overlapping CJK bigrams.  The
    full CJK span is retained for short names so exact internal terminology is
    still highly discriminative without a server-side tokenizer extension.
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
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


class SearchIndex:
    """PostgreSQL full-text and pgvector implementation of the search port."""

    table_name = "chunk_search_index"

    def __init__(self, settings: Settings, engine: Engine | Any | None = None):
        self.settings = settings
        self.table_name = settings.postgres_search_table
        self.engine = engine or create_engine(settings.database_url, pool_pre_ping=True)

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _source_select() -> str:
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
        return """
            FROM chunk_search_index s
            JOIN chunks c ON c.id = s.chunk_id
            JOIN document_versions v ON v.id = c.version_id
            JOIN documents d ON d.id = v.document_id
        """

    @staticmethod
    def _hit(row: Any, score: float | None = None) -> dict[str, Any]:
        mapping = dict(row._mapping if hasattr(row, "_mapping") else row)
        mapping.pop("source_chunk_id", None)
        raw_score = float(score if score is not None else mapping.pop("score", 0.0) or 0.0)
        chunk_id = str(mapping.get("chunk_id") or "")
        return {"_id": chunk_id, "_score": raw_score, "_source": mapping}

    @staticmethod
    def _status_clause(statuses: list[str], params: dict[str, Any]) -> str:
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
        statement = text(sql)
        for name in expanding or []:
            statement = statement.bindparams(bindparam(name, expanding=True))
        return statement

    def current_write_index(self) -> str:
        return f"postgresql:{self.table_name}:{self.settings.embedding_fingerprint}"

    def current_read_index(self) -> str:
        return self.current_write_index()

    def trace_index_name(self) -> str:
        return self.current_read_index()

    def ensure_index(self) -> None:
        """Fail startup when migrations or required PostgreSQL extensions are missing."""

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT to_regclass(:table_name) IS NOT NULL AS table_ready, "
                    "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') AS vector_ready, "
                    "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') AS trgm_ready"
                ),
                {"table_name": self.table_name},
            ).mappings().one()
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
        self.ensure_index()

    def activate_alias(self, _: str | None = None) -> None:
        # PostgreSQL updates are transactional; no external alias switch exists.
        self.ensure_index()

    @staticmethod
    def _document_text(document: dict[str, Any]) -> tuple[str, str, list[str]]:
        filename = str(document.get("filename") or "")
        title = str(document.get("title_path") or document.get("heading_path") or "")
        content = str(document.get("content") or "")
        raw_text = normalize_search_text(f"{filename} {title} {content}")
        # Preserve repetitions after tokenization. lexical_tokens intentionally
        # deduplicates one field, so repeating the raw field before tokenizing
        # would silently discard the intended filename/title weight.
        filename_tokens = lexical_tokens(filename)
        title_tokens = lexical_tokens(title)
        content_tokens = lexical_tokens(content)
        lexical_text = " ".join(
            filename_tokens * 4 + title_tokens * 3 + content_tokens
        )
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
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM chunk_search_index s USING chunks c "
                    "WHERE s.chunk_id = c.id AND c.version_id = :version_id"
                ),
                {"version_id": version_id},
            )

    def clear(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM chunk_search_index"))

    def delete_stale_version_chunks(self, version_id: str, current_chunk_ids: list[str]) -> None:
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
        """Remove rows whose source chunk is no longer an active searchable version."""

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
        normalized = normalize_search_text(query)
        tokens = lexical_tokens(normalized, limit=64)
        if not tokens:
            return []
        params: dict[str, Any] = {
            # websearch_to_tsquery is deliberately used instead of to_tsquery:
            # user-facing identifiers can contain '-' or '/', and malformed
            # tsquery syntax must never turn a normal knowledge query into 500.
            "ts_query": " OR ".join(f'"{token}"' for token in tokens),
            "raw_query": normalized,
            "top_k": top_k,
        }
        scope, expanding = self._scope_clause(
            project_ids, statuses, principal_ids, params
        )
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
            exact_score = "CASE WHEN s.exact_terms && CAST(:exact_terms AS text[]) THEN 100.0 ELSE 0.0 END"
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
        params: dict[str, Any] = {
            "embedding": _vector_literal(vector),
            "embedding_fingerprint": self.settings.embedding_fingerprint,
            "top_k": top_k,
        }
        scope, expanding = self._scope_clause(
            project_ids, statuses, principal_ids, params
        )
        threshold = ""
        if self.settings.vector_min_similarity is not None:
            params["minimum_similarity"] = self.settings.vector_min_similarity
            threshold = (
                "AND (1.0 - (s.embedding <=> CAST(:embedding AS vector))) "
                ">= :minimum_similarity"
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
        if not document_ids:
            return []
        params: dict[str, Any] = {
            "document_ids": list(dict.fromkeys(document_ids)),
            "top_k": top_k,
        }
        scope, expanding = self._scope_clause(
            project_ids, statuses, principal_ids, params
        )
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
        del target_index
        with self.engine.connect() as connection:
            return int(connection.scalar(text("SELECT count(*) FROM chunk_search_index")) or 0)

    def version_records(self, version_id: str) -> list[tuple[int, str, str]]:
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
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def status(self) -> dict[str, Any]:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT current_setting('server_version') AS server_version, "
                        "(SELECT extversion FROM pg_extension WHERE extname = 'vector') "
                        "AS vector_version, "
                        "(SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm') "
                        "AS trgm_version, "
                        "to_regclass(:table_name) IS NOT NULL AS table_ready"
                    ),
                    {"table_name": self.table_name},
                ).mappings().one()
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
