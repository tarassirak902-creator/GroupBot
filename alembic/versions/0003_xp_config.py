"""add xp configuration

Revision ID: 0003_xp_config
Revises: 0002_group_settings
Create Date: 2026-08-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_xp_config"
down_revision: str | None = "0002_group_settings"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "xp_configs",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("xp_per_message", sa.Integer(), nullable=True),
        sa.Column("level_thresholds", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("xp_configs")
