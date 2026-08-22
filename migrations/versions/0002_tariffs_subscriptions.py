"""tariffs and subscriptions

Revision ID: 0002_tariffs_subscriptions
Revises: 0001_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_tariffs_subscriptions"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TEST_LIMITS = {
    "main_groups": 1,
    "network_test_groups": 1,
    "networks": 1,
    "blocked_words": 3,
    "blocked_phrases": 3,
    "auto_repeats": 1,
    "custom_reasons": 3,
    "templates": 1,
    "auto_messages": 1,
    "custom_achievements": 1,
    "exports": 2,
    "reserve_admins": 1,
    "log_groups": 1,
    "custom_vip_rp": 3,
    "automatic_roles": 3,
    "admin_ranks": 3,
    "protection_schedules": 1,
}


def upgrade() -> None:
    op.create_table(
        "tariffs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("duration_days", sa.Integer()),
        sa.Column("max_members_per_group", sa.Integer()),
        sa.Column("max_groups", sa.Integer()),
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("limits_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tariff_id", sa.BigInteger(), sa.ForeignKey("tariffs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_subscriptions_owner_user_id", "subscriptions", ["owner_user_id"])
    op.create_index("ix_subscriptions_tariff_id", "subscriptions", ["tariff_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_ends_at", "subscriptions", ["ends_at"])

    tariffs = sa.table(
        "tariffs",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("duration_days", sa.Integer),
        sa.column("max_members_per_group", sa.Integer),
        sa.column("max_groups", sa.Integer),
        sa.column("is_trial", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("limits_json", sa.JSON),
    )
    op.bulk_insert(
        tariffs,
        [
            {"code": "TEST", "name": "TEST", "duration_days": 3, "max_members_per_group": None, "max_groups": 1, "is_trial": True, "is_active": True, "limits_json": TEST_LIMITS},
            {"code": "BASIC", "name": "BASIC", "duration_days": None, "max_members_per_group": 15000, "max_groups": 1, "is_trial": False, "is_active": True, "limits_json": {}},
            {"code": "STANDARD", "name": "STANDARD", "duration_days": None, "max_members_per_group": 50000, "max_groups": 3, "is_trial": False, "is_active": True, "limits_json": {}},
            {"code": "PRO", "name": "PRO", "duration_days": None, "max_members_per_group": 100000, "max_groups": 10, "is_trial": False, "is_active": True, "limits_json": {}},
            {"code": "MAX", "name": "MAX", "duration_days": None, "max_members_per_group": 200000, "max_groups": 20, "is_trial": False, "is_active": True, "limits_json": {}},
        ],
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_table("tariffs")
