"""add moderation warnings and whitelist

Revision ID: 0010_moderation_warnings_whitelist
Revises: 0009_moderation_core
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
revision: str = "0010_moderation_warnings_whitelist"
down_revision: str | None = "0009_moderation_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table("moderation_warnings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("filter_set_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["filter_set_id"], ["filter_sets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_moderation_warnings_chat_user", "moderation_warnings", ["chat_id", "user_id"], unique=False)
    op.create_table("moderation_whitelist",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_moderation_whitelist_chat_user"))

def downgrade() -> None:
    op.drop_table("moderation_whitelist")
    op.drop_index("ix_moderation_warnings_chat_user", table_name="moderation_warnings")
    op.drop_table("moderation_warnings")
