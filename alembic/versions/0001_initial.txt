"""Initial PostgreSQL schema, consolidated through hierarchical retrieval.

Frozen baseline for new databases; independent of future application model changes.
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "embedding_models",
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("similarity", sa.String(length=30), nullable=False),
        sa.Column("preprocess_version", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fingerprint"),
    )
    op.create_table(
        "index_generations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("physical_index", sa.String(length=500), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("embedding_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("expected_chunks", sa.Integer(), nullable=False),
        sa.Column("indexed_chunks", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("physical_index"),
    )
    op.create_index("ix_index_generations_status", "index_generations", ["status"], unique=False)
    op.create_table(
        "model_usage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=100), nullable=False),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_status", sa.String(length=40), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_usage_model_id", "model_usage", ["model_id"], unique=False)
    op.create_index("ix_model_usage_request_id", "model_usage", ["request_id"], unique=False)
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("aggregate_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"], unique=False
    )
    op.create_index(
        "ix_outbox_events_available_at", "outbox_events", ["available_at"], unique=False
    )
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"], unique=False)
    op.create_index("ix_outbox_events_lease_until", "outbox_events", ["lease_until"], unique=False)
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"], unique=False)
    op.create_table(
        "principals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_type", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=300), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_projects_name", "projects", ["name"], unique=False)
    op.create_table(
        "query_traces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_query", sa.Text(), nullable=False),
        sa.Column("project_ids", sa.JSON(), nullable=False),
        sa.Column("index_name", sa.String(length=500), nullable=False),
        sa.Column("retrieval_json", sa.JSON(), nullable=False),
        sa.Column("answer_status", sa.String(length=40), nullable=True),
        sa.Column("model_id", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id"),
    )
    op.create_index("ix_query_traces_trace_id", "query_traces", ["trace_id"], unique=False)
    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("document_type", sa.String(length=100), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("visibility", sa.String(length=30), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("logical_key", sa.String(length=500), nullable=False),
        sa.Column("external_source_id", sa.String(length=1000), nullable=True),
        sa.Column("normalized_filename", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_project_logical_key", "documents", ["project_id", "logical_key"], unique=True
    )
    op.create_index("ix_documents_is_deleted", "documents", ["is_deleted"], unique=False)
    op.create_index(
        "ix_documents_normalized_filename_trgm",
        "documents",
        ["normalized_filename"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_filename": "gin_trgm_ops"},
    )
    op.create_index("ix_documents_project_id", "documents", ["project_id"], unique=False)
    op.create_index("ix_documents_visibility", "documents", ["visibility"], unique=False)
    op.create_table(
        "embedding_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("object_uri", sa.String(length=1200), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding_input_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["embedding_fingerprint"],
            ["embedding_models.fingerprint"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_embedding_cache_content_hash", "embedding_cache", ["content_hash"], unique=False
    )
    op.create_index(
        "ix_embedding_cache_embedding_fingerprint",
        "embedding_cache",
        ["embedding_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_embedding_cache_embedding_input_hash",
        "embedding_cache",
        ["embedding_input_hash"],
        unique=False,
    )
    op.create_index(
        "ix_embedding_input_fingerprint",
        "embedding_cache",
        ["embedding_input_hash", "embedding_fingerprint"],
        unique=True,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer_status", sa.String(length=40), nullable=True),
        sa.Column("model_id", sa.String(length=100), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=True),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"], unique=False)
    op.create_index("ix_messages_trace_id", "messages", ["trace_id"], unique=False)
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("rating", sa.String(length=30), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_answer_feedback_message_id", "answer_feedback", ["message_id"], unique=False
    )
    op.create_table(
        "document_acl",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("permission", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_acl_document_principal", "document_acl", ["document_id", "principal_id"], unique=True
    )
    op.create_index("ix_document_acl_document_id", "document_acl", ["document_id"], unique=False)
    op.create_index("ix_document_acl_principal_id", "document_acl", ["principal_id"], unique=False)
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=1000), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=30), nullable=False),
        sa.Column("version_label", sa.String(length=100), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_document_id", sa.String(length=36), nullable=True),
        sa.Column("parse_warnings", sa.JSON(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "technical_status", sa.String(length=40), server_default="received", nullable=False
        ),
        sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_version_id", sa.String(length=36), nullable=True),
        sa.Column("parser_fingerprint", sa.String(length=200), nullable=True),
        sa.Column("chunker_fingerprint", sa.String(length=200), nullable=True),
        sa.Column("searchable_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_version_hash", "document_versions", ["document_id", "sha256"], unique=True
    )
    op.create_index(
        "ix_document_versions_document_id", "document_versions", ["document_id"], unique=False
    )
    op.create_index(
        "ix_document_versions_is_current", "document_versions", ["is_current"], unique=False
    )
    op.create_index(
        "ix_document_versions_lifecycle_status",
        "document_versions",
        ["lifecycle_status"],
        unique=False,
    )
    op.create_index("ix_document_versions_sha256", "document_versions", ["sha256"], unique=False)
    op.create_index(
        "ix_document_versions_technical_status",
        "document_versions",
        ["technical_status"],
        unique=False,
    )
    op.create_index(
        "uq_document_current_approved",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND lifecycle_status = 'approved'"),
    )
    op.create_table(
        "document_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("object_uri", sa.String(length=1200), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artifact_version_type_fingerprint",
        "document_artifacts",
        ["version_id", "artifact_type", "fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_document_artifacts_version_id", "document_artifacts", ["version_id"], unique=False
    )
    op.create_table(
        "document_sections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("parent_section_id", sa.String(length=36), nullable=True),
        sa.Column("section_key", sa.String(length=500), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("normalized_title", sa.String(length=1000), nullable=False),
        sa.Column("heading_path", sa.String(length=2000), server_default="", nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["parent_section_id"], ["document_sections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_section_version_key",
        "document_sections",
        ["version_id", "section_key"],
        unique=True,
    )
    op.create_index(
        "ix_document_section_version_ordinal",
        "document_sections",
        ["version_id", "ordinal"],
        unique=True,
    )
    op.create_index(
        "ix_document_sections_parent_section_id",
        "document_sections",
        ["parent_section_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_sections_version_id", "document_sections", ["version_id"], unique=False
    )
    op.create_table(
        "index_sync_state",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("expected_chunks", sa.Integer(), nullable=False),
        sa.Column("indexed_chunks", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["index_generations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_index_sync_state_generation_id", "index_sync_state", ["generation_id"], unique=False
    )
    op.create_index("ix_index_sync_state_status", "index_sync_state", ["status"], unique=False)
    op.create_index(
        "ix_index_sync_state_version_id", "index_sync_state", ["version_id"], unique=False
    )
    op.create_index(
        "ix_sync_version_generation",
        "index_sync_state",
        ["version_id", "generation_id"],
        unique=True,
    )
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("stage", sa.String(length=50), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"], unique=False
    )
    op.create_index(
        "ix_ingestion_jobs_lease_until", "ingestion_jobs", ["lease_until"], unique=False
    )
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"], unique=False)
    op.create_index("ix_ingestion_jobs_version_id", "ingestion_jobs", ["version_id"], unique=False)
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.String(length=1000), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("sheet_name", sa.String(length=300), nullable=True),
        sa.Column("cell_range", sa.String(length=100), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("parent_chunk_id", sa.String(length=36), nullable=True),
        sa.Column("previous_chunk_id", sa.String(length=36), nullable=True),
        sa.Column("next_chunk_id", sa.String(length=36), nullable=True),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("embedding_input_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["document_sections.id"],
            name="fk_chunks_section_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chunk_version_ordinal", "chunks", ["version_id", "ordinal"], unique=True)
    op.create_index("ix_chunks_content_hash", "chunks", ["content_hash"], unique=False)
    op.create_index(
        "ix_chunks_embedding_input_hash", "chunks", ["embedding_input_hash"], unique=False
    )
    op.create_index("ix_chunks_record_hash", "chunks", ["record_hash"], unique=False)
    op.create_index("ix_chunks_section_id", "chunks", ["section_id"], unique=False)
    op.create_index("ix_chunks_version_id", "chunks", ["version_id"], unique=False)
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("embedding_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cache_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cache_id"],
            ["embedding_cache.id"],
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["embedding_fingerprint"],
            ["embedding_models.fingerprint"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chunk_embedding_unique",
        "chunk_embeddings",
        ["chunk_id", "embedding_fingerprint"],
        unique=True,
    )
    op.create_index("ix_chunk_embeddings_cache_id", "chunk_embeddings", ["cache_id"], unique=False)
    op.create_index("ix_chunk_embeddings_chunk_id", "chunk_embeddings", ["chunk_id"], unique=False)
    op.create_index(
        "ix_chunk_embeddings_embedding_fingerprint",
        "chunk_embeddings",
        ["embedding_fingerprint"],
        unique=False,
    )
    op.create_table(
        "chunk_search_index",
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("embedding_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("lexical_text", sa.Text(), nullable=False),
        sa.Column("exact_terms", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("filename_normalized", sa.Text(), server_default="", nullable=False),
        sa.Column("filename_tokens", sa.Text(), server_default="", nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("section_path", sa.Text(), server_default="", nullable=False),
        sa.Column("embedding_input_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index(
        "ix_chunk_search_embedding_fingerprint",
        "chunk_search_index",
        ["embedding_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_chunk_search_record_hash", "chunk_search_index", ["record_hash"], unique=False
    )
    op.create_index(
        "ix_chunk_search_section_id", "chunk_search_index", ["section_id"], unique=False
    )
    op.execute("ALTER TABLE chunk_search_index ADD COLUMN embedding halfvec(2560)")
    op.execute(
        "ALTER TABLE chunk_search_index ADD COLUMN search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED"
    )
    op.execute("CREATE INDEX ix_chunk_search_fts ON chunk_search_index USING gin (search_vector)")
    op.execute(
        "CREATE INDEX ix_chunk_search_exact_terms ON chunk_search_index USING gin (exact_terms)"
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_raw_trgm ON chunk_search_index USING gin (raw_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_embedding_hnsw ON chunk_search_index USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 128) WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    # Extensions may be shared with other schemas and are intentionally retained.
    op.drop_table("chunk_search_index")
    op.drop_table("chunk_embeddings")
    op.drop_table("chunks")
    op.drop_table("ingestion_jobs")
    op.drop_table("index_sync_state")
    op.drop_table("document_sections")
    op.drop_table("document_artifacts")
    op.drop_table("document_versions")
    op.drop_table("document_acl")
    op.drop_table("answer_feedback")
    op.drop_table("messages")
    op.drop_table("embedding_cache")
    op.drop_table("documents")
    op.drop_table("query_traces")
    op.drop_table("projects")
    op.drop_table("principals")
    op.drop_table("outbox_events")
    op.drop_table("model_usage")
    op.drop_table("index_generations")
    op.drop_table("embedding_models")
    op.drop_table("conversations")
