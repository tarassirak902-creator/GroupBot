"""add group settings

Revision ID: 0002_group_settings
Revises: 0001_initial_core
Create Date: 2026-08-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_group_settings"
down_revision: str | None = "0001_initial_core"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_settings",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("rp_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("xp_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("economy_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_activity_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("moderation_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("group_settings")
