"""add subscription addon limits

Revision ID: 0008_subscription_addons
Revises: 0007_networks
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008_subscription_addons"
down_revision: str | None = "0007_networks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription_addons",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("limit_key", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_subscription_addon_quantity_positive"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "limit_key", name="uq_subscription_addon_limit_key"),
    )
    op.create_index(
        "ix_subscription_addons_subscription_id",
        "subscription_addons",
        ["subscription_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_addons_subscription_id", table_name="subscription_addons")
    op.drop_table("subscription_addons")
