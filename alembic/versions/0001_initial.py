"""Initial knowledge base schema."""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("projects", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(200), nullable=False, unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_projects_name", "projects", ["name"])
    op.create_table("conversations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("principals", sa.Column("id", sa.String(36), primary_key=True), sa.Column("principal_type", sa.String(30), nullable=False), sa.Column("external_id", sa.String(300), nullable=False, unique=True))
    op.create_table("documents", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False), sa.Column("filename", sa.String(500), nullable=False), sa.Column("document_type", sa.String(100), nullable=False), sa.Column("owner", sa.String(200)), sa.Column("source_type", sa.String(50), nullable=False), sa.Column("visibility", sa.String(30), nullable=False), sa.Column("is_deleted", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_documents_project_id", "documents", ["project_id"])
    op.create_index("ix_documents_visibility", "documents", ["visibility"])
    op.create_index("ix_documents_is_deleted", "documents", ["is_deleted"])
    op.create_table("document_versions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("storage_path", sa.String(1000), nullable=False), sa.Column("lifecycle_status", sa.String(30), nullable=False), sa.Column("version_label", sa.String(100)), sa.Column("effective_at", sa.DateTime(timezone=True)), sa.Column("supersedes_document_id", sa.String(36)), sa.Column("parse_warnings", sa.JSON(), nullable=False), sa.Column("indexed_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_sha256", "document_versions", ["sha256"])
    op.create_index("ix_document_versions_lifecycle_status", "document_versions", ["lifecycle_status"])
    op.create_index("ix_document_version_hash", "document_versions", ["document_id", "sha256"], unique=True)
    op.create_table("chunks", sa.Column("id", sa.String(36), primary_key=True), sa.Column("version_id", sa.String(36), sa.ForeignKey("document_versions.id"), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("heading_path", sa.String(1000)), sa.Column("page_number", sa.Integer()), sa.Column("sheet_name", sa.String(300)), sa.Column("cell_range", sa.String(100)), sa.Column("content", sa.Text(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("token_count", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_chunks_version_id", "chunks", ["version_id"])
    op.create_index("ix_chunks_content_hash", "chunks", ["content_hash"])
    op.create_index("ix_chunk_version_ordinal", "chunks", ["version_id", "ordinal"], unique=True)
    op.create_table("ingestion_jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False), sa.Column("version_id", sa.String(36), sa.ForeignKey("document_versions.id"), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("stage", sa.String(50), nullable=False), sa.Column("progress", sa.Integer(), nullable=False), sa.Column("error_message", sa.Text()), sa.Column("warnings", sa.JSON(), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("ix_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"])
    op.create_index("ix_ingestion_jobs_version_id", "ingestion_jobs", ["version_id"])
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])
    op.create_table("messages", sa.Column("id", sa.String(36), primary_key=True), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("role", sa.String(20), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("answer_status", sa.String(40)), sa.Column("model_id", sa.String(100)), sa.Column("trace_id", sa.String(36)), sa.Column("citations", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_trace_id", "messages", ["trace_id"])
    op.create_table("answer_feedback", sa.Column("id", sa.String(36), primary_key=True), sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id"), nullable=False), sa.Column("rating", sa.String(30), nullable=False), sa.Column("comment", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_answer_feedback_message_id", "answer_feedback", ["message_id"])
    op.create_table("model_usage", sa.Column("id", sa.String(36), primary_key=True), sa.Column("model_id", sa.String(100), nullable=False), sa.Column("request_id", sa.String(100), nullable=False), sa.Column("prompt_tokens", sa.Integer(), nullable=False), sa.Column("completion_tokens", sa.Integer(), nullable=False), sa.Column("total_tokens", sa.Integer(), nullable=False), sa.Column("called_at", sa.DateTime(timezone=True), nullable=False), sa.Column("result_status", sa.String(40), nullable=False), sa.Column("latency_ms", sa.Float(), nullable=False), sa.Column("error_code", sa.String(100)))
    op.create_index("ix_model_usage_model_id", "model_usage", ["model_id"])
    op.create_index("ix_model_usage_request_id", "model_usage", ["request_id"])
    op.create_table("document_acl", sa.Column("id", sa.String(36), primary_key=True), sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False), sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), nullable=False), sa.Column("permission", sa.String(20), nullable=False))
    op.create_index("ix_document_acl_document_id", "document_acl", ["document_id"])
    op.create_index("ix_document_acl_principal_id", "document_acl", ["principal_id"])
    op.create_index("ix_acl_document_principal", "document_acl", ["document_id", "principal_id"], unique=True)


def downgrade() -> None:
    for table in ["document_acl", "model_usage", "answer_feedback", "messages", "ingestion_jobs", "chunks", "document_versions", "documents", "principals", "conversations", "projects"]:
        op.drop_table(table)

