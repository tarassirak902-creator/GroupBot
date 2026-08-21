"""add rp core

Revision ID: 0006_rp_core
Revises: 0005_economy_transactions
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0006_rp_core"
down_revision: str | None = "0005_economy_transactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rp_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("emoji", sa.String(16), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("chat_id", "code", name="uq_rp_actions_chat_code"),
    )
    op.create_table(
        "cooldowns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("chat_id", "user_id", "key", name="uq_cooldown_scope"),
    )


def downgrade() -> None:
    op.drop_table("cooldowns")
    op.drop_table("rp_actions")
