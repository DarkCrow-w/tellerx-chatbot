"""Persist MinIO knowledge scan progress."""

import sqlalchemy as sa

from alembic import op

revision = "0002_knowledge_scans"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_scans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(30), nullable=False),
        *[
            sa.Column(name, sa.Integer(), nullable=False)
            for name in ["processed", "imported", "skipped", "failed"]
        ],
        sa.Column("current_key", sa.String(1200)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_scans_project_id", "knowledge_scans", ["project_id"])
    op.create_index(
        "uq_knowledge_scan_running",
        "knowledge_scans",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade():
    op.drop_table("knowledge_scans")
