"""Add PostgreSQL full-text and pgvector chunk search storage."""

import sqlalchemy as sa

from alembic import op

revision = "0004_postgresql_pgvector_fts"
down_revision = "0003_elasticsearch_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "chunk_search_index",
        sa.Column(
            "chunk_id",
            sa.String(36),
            sa.ForeignKey("chunks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("embedding_fingerprint", sa.String(64)),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("lexical_text", sa.Text(), nullable=False),
        sa.Column("exact_terms", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute("ALTER TABLE chunk_search_index ADD COLUMN embedding vector(1024)")
    op.execute(
        "ALTER TABLE chunk_search_index ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED"
    )
    op.create_index(
        "ix_chunk_search_embedding_fingerprint",
        "chunk_search_index",
        ["embedding_fingerprint"],
    )
    op.create_index(
        "ix_chunk_search_record_hash",
        "chunk_search_index",
        ["record_hash"],
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_fts ON chunk_search_index "
        "USING gin (search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_exact_terms ON chunk_search_index "
        "USING gin (exact_terms)"
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_raw_trgm ON chunk_search_index "
        "USING gin (raw_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_embedding_hnsw ON chunk_search_index "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 128) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_table("chunk_search_index")
