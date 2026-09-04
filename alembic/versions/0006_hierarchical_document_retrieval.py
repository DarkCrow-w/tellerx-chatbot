"""Add document scopes, section trees, and contextual embedding identities."""

import sqlalchemy as sa

from alembic import op

revision = "0006_hierarchical_retrieval"
down_revision = "0005_qwen_embedding_2560"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("normalized_filename", sa.String(500)))
    op.execute(
        "UPDATE documents SET normalized_filename = lower(regexp_replace("
        "regexp_replace(normalize(filename, NFKC), '\\.[^.]+$', ''), "
        "'[_\\-[:space:]（）()\\[\\]【】{}]+', ' ', 'g'))"
    )
    op.alter_column("documents", "normalized_filename", nullable=False)
    op.create_index(
        "ix_documents_normalized_filename_trgm",
        "documents",
        ["normalized_filename"],
        postgresql_using="gin",
        postgresql_ops={"normalized_filename": "gin_trgm_ops"},
    )

    op.create_table(
        "document_sections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(36),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_section_id",
            sa.String(36),
            sa.ForeignKey("document_sections.id", ondelete="CASCADE"),
        ),
        sa.Column("section_key", sa.String(500), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("normalized_title", sa.String(1000), nullable=False),
        sa.Column("heading_path", sa.String(2000), nullable=False, server_default=""),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_document_sections_version_id", "document_sections", ["version_id"])
    op.create_index(
        "ix_document_sections_parent_section_id", "document_sections", ["parent_section_id"]
    )
    op.create_index(
        "ix_document_section_version_ordinal",
        "document_sections",
        ["version_id", "ordinal"],
        unique=True,
    )
    op.create_index(
        "ix_document_section_version_key",
        "document_sections",
        ["version_id", "section_key"],
        unique=True,
    )
    op.execute(
        "INSERT INTO document_sections "
        "(id, version_id, parent_section_id, section_key, level, title, normalized_title, "
        "heading_path, ordinal, page_start, page_end, created_at) "
        "SELECT 'root-' || substr(md5(v.id || '-root'), 1, 31), v.id, NULL, 'root', 0, "
        "d.filename, d.normalized_filename, '', 0, min(c.page_number), max(c.page_number), now() "
        "FROM document_versions v JOIN documents d ON d.id = v.document_id "
        "LEFT JOIN chunks c ON c.version_id = v.id GROUP BY v.id, d.filename, d.normalized_filename"
    )

    op.add_column("chunks", sa.Column("section_id", sa.String(36)))
    op.create_foreign_key(
        "fk_chunks_section_id",
        "chunks",
        "document_sections",
        ["section_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_chunks_section_id", "chunks", ["section_id"])
    op.execute(
        "UPDATE chunks c SET section_id = s.id FROM document_sections s "
        "WHERE s.version_id = c.version_id AND s.section_key = 'root'"
    )
    op.add_column("chunks", sa.Column("embedding_input_hash", sa.String(64)))
    op.execute("UPDATE chunks SET embedding_input_hash = content_hash")
    op.alter_column("chunks", "embedding_input_hash", nullable=False)
    op.create_index("ix_chunks_embedding_input_hash", "chunks", ["embedding_input_hash"])

    op.add_column("embedding_cache", sa.Column("embedding_input_hash", sa.String(64)))
    op.execute("UPDATE embedding_cache SET embedding_input_hash = content_hash")
    op.alter_column("embedding_cache", "embedding_input_hash", nullable=False)
    op.create_index(
        "ix_embedding_cache_embedding_input_hash", "embedding_cache", ["embedding_input_hash"]
    )
    op.drop_index("ix_embedding_content_fingerprint", table_name="embedding_cache")
    op.create_index(
        "ix_embedding_input_fingerprint",
        "embedding_cache",
        ["embedding_input_hash", "embedding_fingerprint"],
        unique=True,
    )

    op.add_column(
        "chunk_search_index",
        sa.Column("filename_normalized", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "chunk_search_index",
        sa.Column("filename_tokens", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("chunk_search_index", sa.Column("section_id", sa.String(36)))
    op.add_column(
        "chunk_search_index",
        sa.Column("section_path", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("chunk_search_index", sa.Column("embedding_input_hash", sa.String(64)))
    op.execute(
        "UPDATE chunk_search_index s SET "
        "filename_normalized = d.normalized_filename, "
        "filename_tokens = d.normalized_filename, section_id = c.section_id, "
        "section_path = coalesce(c.heading_path, ''), "
        "embedding_input_hash = c.embedding_input_hash "
        "FROM chunks c JOIN document_versions v ON v.id = c.version_id "
        "JOIN documents d ON d.id = v.document_id WHERE c.id = s.chunk_id"
    )
    op.create_index("ix_chunk_search_section_id", "chunk_search_index", ["section_id"])


def downgrade() -> None:
    op.drop_index("ix_chunk_search_section_id", table_name="chunk_search_index")
    for column in (
        "embedding_input_hash",
        "section_path",
        "section_id",
        "filename_tokens",
        "filename_normalized",
    ):
        op.drop_column("chunk_search_index", column)

    op.drop_index("ix_embedding_input_fingerprint", table_name="embedding_cache")
    op.drop_index("ix_embedding_cache_embedding_input_hash", table_name="embedding_cache")
    op.drop_column("embedding_cache", "embedding_input_hash")
    op.create_index(
        "ix_embedding_content_fingerprint",
        "embedding_cache",
        ["content_hash", "embedding_fingerprint"],
        unique=True,
    )

    op.drop_index("ix_chunks_embedding_input_hash", table_name="chunks")
    op.drop_column("chunks", "embedding_input_hash")
    op.drop_index("ix_chunks_section_id", table_name="chunks")
    op.drop_constraint("fk_chunks_section_id", "chunks", type_="foreignkey")
    op.drop_column("chunks", "section_id")
    op.drop_table("document_sections")
    op.drop_index("ix_documents_normalized_filename_trgm", table_name="documents")
    op.drop_column("documents", "normalized_filename")
