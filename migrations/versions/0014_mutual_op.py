"""add mutual OP advertising tables

Revision ID: 0014_mutual_op
Revises: 0013_advertising_marketplace
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_mutual_op"
down_revision: str | None = "0013_advertising_marketplace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "advertising_mutual_op_directions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.BigInteger(), sa.ForeignKey("advertising_deals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_title", sa.String(255), nullable=False),
        sa.Column("target_title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("invite_link", sa.String(512)),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("deal_id", "source_chat_id", name="uq_mutual_op_deal_source"),
    )
    for column in ["deal_id", "source_chat_id", "target_chat_id", "status", "starts_at", "ends_at", "completed_at"]:
        op.create_index(f"ix_advertising_mutual_op_directions_{column}", "advertising_mutual_op_directions", [column])

    op.create_table(
        "advertising_mutual_op_members",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("direction_id", sa.BigInteger(), sa.ForeignKey("advertising_mutual_op_directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("direction_id", "user_id", name="uq_mutual_op_direction_user"),
    )
    for column in ["direction_id", "user_id", "is_active", "left_at"]:
        op.create_index(f"ix_advertising_mutual_op_members_{column}", "advertising_mutual_op_members", [column])


def downgrade() -> None:
    op.drop_table("advertising_mutual_op_members")
    op.drop_table("advertising_mutual_op_directions")
