"""add moderation core

Revision ID: 0009_moderation_core
Revises: 0008_auto_activity
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0009_moderation_core"
down_revision: str | None = "0008_auto_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "filter_sets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("match_type", sa.String(16), server_default="whole", nullable=False),
        sa.Column("case_sensitive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("delete_message", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("action", sa.String(32), server_default="delete", nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("exclude_admins", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("chat_id", "name", name="uq_filter_sets_chat_name"),
    )
    op.create_table(
        "filter_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("filter_set_id", sa.BigInteger(), nullable=False),
        sa.Column("value", sa.String(500), nullable=False),
        sa.ForeignKeyConstraint(["filter_set_id"], ["filter_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("filter_set_id", "value", name="uq_filter_item_value"),
    )
    op.create_table(
        "moderation_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("filter_set_id", sa.BigInteger(), nullable=False),
        sa.Column("filter_item_id", sa.BigInteger(), nullable=False),
        sa.Column("matched_value", sa.String(500), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("telegram_ok", sa.Boolean(), nullable=True),
        sa.Column("telegram_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["filter_set_id"], ["filter_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["filter_item_id"], ["filter_items.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_moderation_actions_chat_created", "moderation_actions", ["chat_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_moderation_actions_chat_created", table_name="moderation_actions")
    op.drop_table("moderation_actions")
    op.drop_table("filter_items")
    op.drop_table("filter_sets")
