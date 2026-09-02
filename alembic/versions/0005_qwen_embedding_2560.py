"""Upgrade the PostgreSQL search projection to 2560-dimensional embeddings."""

from alembic import op

revision = "0005_qwen_embedding_2560"
down_revision = "0004_postgresql_pgvector_fts"
branch_labels = None
depends_on = None


def _replace_embedding_column(
    dimensions: int,
    *,
    column_type: str,
    operator_class: str,
) -> None:
    """保留词法投影，只替换无法跨维度转换的向量列和 HNSW 索引。"""

    op.execute("DROP INDEX IF EXISTS ix_chunk_search_embedding_hnsw")
    # 指纹只描述当前行中实际存在的向量；清空后不能继续标记为旧向量空间。
    op.execute("UPDATE chunk_search_index SET embedding_fingerprint = NULL")
    op.execute("ALTER TABLE chunk_search_index DROP COLUMN embedding")
    op.execute(
        "ALTER TABLE chunk_search_index "
        f"ADD COLUMN embedding {column_type}({dimensions})"
    )
    op.execute(
        "CREATE INDEX ix_chunk_search_embedding_hnsw ON chunk_search_index "
        f"USING hnsw (embedding {operator_class}) "
        "WITH (m = 16, ef_construction = 128) "
        "WHERE embedding IS NOT NULL"
    )


def upgrade() -> None:
    """切换到公司 qwen3-embedding 默认的 2560 维向量空间。"""

    # vector 的 HNSW 索引最多支持 2000 维；halfvec 可覆盖 2560 维并保留近似检索。
    _replace_embedding_column(
        2560,
        column_type="halfvec",
        operator_class="halfvec_cosine_ops",
    )


def downgrade() -> None:
    """仅恢复 1024 维列结构；旧向量仍需使用对应模型重新生成。"""

    _replace_embedding_column(
        1024,
        column_type="vector",
        operator_class="vector_cosine_ops",
    )
