"""Track prompt version for every model usage row."""

import sqlalchemy as sa

from alembic import op

revision = "0002_model_usage_prompt_version"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_usage", sa.Column("prompt_version", sa.String(100)))


def downgrade() -> None:
    op.drop_column("model_usage", "prompt_version")
