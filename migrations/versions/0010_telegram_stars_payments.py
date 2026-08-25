"""add telegram stars payments

Revision ID: 0010_telegram_stars_payments
Revises: 0009_network_names
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_telegram_stars_payments"
down_revision: str | None = "0009_network_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_stars_payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_payment_charge_id", sa.String(length=255), nullable=False),
        sa.Column("provider_payment_charge_id", sa.String(length=255), nullable=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tariff_id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("invoice_payload", sa.String(length=255), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), server_default="XTR", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.telegram_user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tariff_id"], ["tariffs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_payment_charge_id"),
    )
    op.create_index(
        op.f("ix_telegram_stars_payments_owner_user_id"),
        "telegram_stars_payments",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_stars_payments_tariff_id"),
        "telegram_stars_payments",
        ["tariff_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_stars_payments_subscription_id"),
        "telegram_stars_payments",
        ["subscription_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_stars_payments_subscription_id"), table_name="telegram_stars_payments")
    op.drop_index(op.f("ix_telegram_stars_payments_tariff_id"), table_name="telegram_stars_payments")
    op.drop_index(op.f("ix_telegram_stars_payments_owner_user_id"), table_name="telegram_stars_payments")
    op.drop_table("telegram_stars_payments")
