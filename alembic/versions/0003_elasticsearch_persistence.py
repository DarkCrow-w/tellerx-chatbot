"""Add durable artifacts, embeddings, outbox, and Elasticsearch generations."""

import sqlalchemy as sa

from alembic import op

revision = "0003_elasticsearch_persistence"
down_revision = "0002_model_usage_prompt_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("logical_key", sa.String(500)))
    op.add_column("documents", sa.Column("external_source_id", sa.String(1000)))
    op.execute("UPDATE documents SET logical_key = filename WHERE logical_key IS NULL")
    op.execute(
        "WITH ranked AS ("
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY project_id, logical_key ORDER BY created_at, id) AS rn "
        "FROM documents) "
        "UPDATE documents SET logical_key = documents.logical_key || '#' || documents.id "
        "FROM ranked WHERE documents.id = ranked.id AND ranked.rn > 1"
    )
    op.alter_column("documents", "logical_key", nullable=False)
    op.create_index("ix_document_project_logical_key", "documents", ["project_id", "logical_key"], unique=True)

    op.add_column("document_versions", sa.Column("technical_status", sa.String(40), server_default="received", nullable=False))
    op.add_column("document_versions", sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("document_versions", sa.Column("effective_to", sa.DateTime(timezone=True)))
    op.add_column("document_versions", sa.Column("supersedes_version_id", sa.String(36)))
    op.add_column("document_versions", sa.Column("parser_fingerprint", sa.String(200)))
    op.add_column("document_versions", sa.Column("chunker_fingerprint", sa.String(200)))
    op.add_column("document_versions", sa.Column("searchable_at", sa.DateTime(timezone=True)))
    op.create_index("ix_document_versions_technical_status", "document_versions", ["technical_status"])
    op.create_index("ix_document_versions_is_current", "document_versions", ["is_current"])
    op.execute("UPDATE document_versions SET technical_status = CASE WHEN indexed_at IS NULL THEN 'received' ELSE 'searchable' END")
    op.execute("UPDATE document_versions SET searchable_at = indexed_at WHERE indexed_at IS NOT NULL")
    op.execute(
        "UPDATE document_versions SET is_current = true "
        "WHERE id = (SELECT v2.id FROM document_versions v2 "
        "WHERE v2.document_id = document_versions.document_id "
        "AND v2.lifecycle_status = 'approved' "
        "ORDER BY v2.created_at DESC, v2.id DESC LIMIT 1)"
    )
    op.create_index(
        "uq_document_current_approved",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND lifecycle_status = 'approved'"),
    )

    op.add_column("chunks", sa.Column("record_hash", sa.String(64)))
    op.add_column("chunks", sa.Column("parent_chunk_id", sa.String(36)))
    op.add_column("chunks", sa.Column("previous_chunk_id", sa.String(36)))
    op.add_column("chunks", sa.Column("next_chunk_id", sa.String(36)))
    op.execute("UPDATE chunks SET record_hash = content_hash WHERE record_hash IS NULL")
    op.alter_column("chunks", "record_hash", nullable=False)
    op.create_index("ix_chunks_record_hash", "chunks", ["record_hash"])

    op.add_column("ingestion_jobs", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.create_index("ix_ingestion_jobs_lease_until", "ingestion_jobs", ["lease_until"])

    op.create_table("document_artifacts", sa.Column("id", sa.String(36), primary_key=True), sa.Column("version_id", sa.String(36), sa.ForeignKey("document_versions.id"), nullable=False), sa.Column("artifact_type", sa.String(40), nullable=False), sa.Column("object_uri", sa.String(1200), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("byte_size", sa.Integer(), nullable=False), sa.Column("fingerprint", sa.String(200), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_document_artifacts_version_id", "document_artifacts", ["version_id"])
    op.create_index("ix_artifact_version_type_fingerprint", "document_artifacts", ["version_id", "artifact_type", "fingerprint"], unique=True)

    op.create_table("embedding_models", sa.Column("fingerprint", sa.String(64), primary_key=True), sa.Column("model_id", sa.String(200), nullable=False), sa.Column("dimensions", sa.Integer(), nullable=False), sa.Column("similarity", sa.String(30), nullable=False), sa.Column("preprocess_version", sa.String(100), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("embedding_cache", sa.Column("id", sa.String(36), primary_key=True), sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("embedding_fingerprint", sa.String(64), sa.ForeignKey("embedding_models.fingerprint"), nullable=False), sa.Column("object_uri", sa.String(1200), nullable=False), sa.Column("checksum", sa.String(64), nullable=False), sa.Column("dimensions", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_embedding_cache_content_hash", "embedding_cache", ["content_hash"])
    op.create_index("ix_embedding_cache_embedding_fingerprint", "embedding_cache", ["embedding_fingerprint"])
    op.create_index("ix_embedding_content_fingerprint", "embedding_cache", ["content_hash", "embedding_fingerprint"], unique=True)
    op.create_table("chunk_embeddings", sa.Column("id", sa.String(36), primary_key=True), sa.Column("chunk_id", sa.String(36), sa.ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False), sa.Column("embedding_fingerprint", sa.String(64), sa.ForeignKey("embedding_models.fingerprint"), nullable=False), sa.Column("cache_id", sa.String(36), sa.ForeignKey("embedding_cache.id"), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_chunk_embeddings_chunk_id", "chunk_embeddings", ["chunk_id"])
    op.create_index("ix_chunk_embeddings_embedding_fingerprint", "chunk_embeddings", ["embedding_fingerprint"])
    op.create_index("ix_chunk_embeddings_cache_id", "chunk_embeddings", ["cache_id"])
    op.create_index("ix_chunk_embedding_unique", "chunk_embeddings", ["chunk_id", "embedding_fingerprint"], unique=True)

    op.create_table("outbox_events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("aggregate_id", sa.String(36), nullable=False), sa.Column("event_type", sa.String(50), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("available_at", sa.DateTime(timezone=True), nullable=False), sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("published_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for column in ["aggregate_id", "event_type", "status", "available_at", "lease_until"]:
        op.create_index(f"ix_outbox_events_{column}", "outbox_events", [column])

    op.create_table("index_generations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("physical_index", sa.String(500), nullable=False, unique=True), sa.Column("schema_version", sa.String(50), nullable=False), sa.Column("embedding_fingerprint", sa.String(64), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("expected_chunks", sa.Integer(), nullable=False), sa.Column("indexed_chunks", sa.Integer(), nullable=False), sa.Column("manifest_hash", sa.String(64)), sa.Column("activated_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_index_generations_status", "index_generations", ["status"])
    op.create_table("index_sync_state", sa.Column("id", sa.String(36), primary_key=True), sa.Column("version_id", sa.String(36), sa.ForeignKey("document_versions.id"), nullable=False), sa.Column("generation_id", sa.String(36), sa.ForeignKey("index_generations.id"), nullable=False), sa.Column("expected_chunks", sa.Integer(), nullable=False), sa.Column("indexed_chunks", sa.Integer(), nullable=False), sa.Column("manifest_hash", sa.String(64), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("verified_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text()))
    op.create_index("ix_index_sync_state_version_id", "index_sync_state", ["version_id"])
    op.create_index("ix_index_sync_state_generation_id", "index_sync_state", ["generation_id"])
    op.create_index("ix_index_sync_state_status", "index_sync_state", ["status"])
    op.create_index("ix_sync_version_generation", "index_sync_state", ["version_id", "generation_id"], unique=True)

    op.create_table("query_traces", sa.Column("id", sa.String(36), primary_key=True), sa.Column("trace_id", sa.String(36), nullable=False, unique=True), sa.Column("normalized_query", sa.Text(), nullable=False), sa.Column("project_ids", sa.JSON(), nullable=False), sa.Column("index_name", sa.String(500), nullable=False), sa.Column("retrieval_json", sa.JSON(), nullable=False), sa.Column("answer_status", sa.String(40)), sa.Column("model_id", sa.String(100)), sa.Column("latency_ms", sa.Float(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_query_traces_trace_id", "query_traces", ["trace_id"])


def downgrade() -> None:
    for table in ["query_traces", "index_sync_state", "index_generations", "outbox_events", "chunk_embeddings", "embedding_cache", "embedding_models", "document_artifacts"]:
        op.drop_table(table)
    op.drop_index("ix_ingestion_jobs_lease_until", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "lease_until")
    op.drop_index("ix_chunks_record_hash", table_name="chunks")
    for column in ["next_chunk_id", "previous_chunk_id", "parent_chunk_id", "record_hash"]:
        op.drop_column("chunks", column)
    for index in ["ix_document_versions_is_current", "ix_document_versions_technical_status"]:
        op.drop_index(index, table_name="document_versions")
    op.drop_index("uq_document_current_approved", table_name="document_versions")
    for column in ["searchable_at", "chunker_fingerprint", "parser_fingerprint", "supersedes_version_id", "effective_to", "is_current", "technical_status"]:
        op.drop_column("document_versions", column)
    op.drop_index("ix_document_project_logical_key", table_name="documents")
    op.drop_column("documents", "external_source_id")
    op.drop_column("documents", "logical_key")
