"""clean MASTER foundation

Revision ID: 0001_foundation
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(255)),
        sa.Column("first_name", sa.String(255)),
        sa.Column("last_name", sa.String(255)),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_account", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "groups",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("bot_added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("connect_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("disconnect_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_groups_connect_deadline_at", "groups", ["connect_deadline_at"])
    op.create_index("ix_groups_disconnect_deadline_at", "groups", ["disconnect_deadline_at"])

    op.create_table(
        "group_owners",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_group_owner_chat_user"),
    )
    op.create_index("ix_group_owners_chat_id", "group_owners", ["chat_id"])
    op.create_index("ix_group_owners_user_id", "group_owners", ["user_id"])

    op.create_table(
        "group_members",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="member"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("joined_at", sa.DateTime(timezone=True)),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_group_member_chat_user"),
    )
    op.create_index("ix_group_members_chat_id", "group_members", ["chat_id"])
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])
    op.create_index("ix_group_members_last_activity_at", "group_members", ["last_activity_at"])

    op.create_table(
        "group_settings",
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rules_text", sa.Text()),
        sa.Column("welcome_config", sa.JSON()),
        sa.Column("moderation_config", sa.JSON()),
        sa.Column("automation_config", sa.JSON()),
        sa.Column("game_config", sa.JSON()),
        sa.Column("advertising_config", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "admin_roles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("chat_id", "name", name="uq_admin_role_chat_name"),
    )
    op.create_index("ix_admin_roles_chat_id", "admin_roles", ["chat_id"])

    op.create_table(
        "admin_permissions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("role_id", sa.BigInteger(), sa.ForeignKey("admin_roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission", sa.String(128), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("role_id", "permission", name="uq_admin_permission_role_key"),
    )
    op.create_index("ix_admin_permissions_role_id", "admin_permissions", ["role_id"])

    op.create_table(
        "admin_assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("role_id", sa.BigInteger(), sa.ForeignKey("admin_roles.id", ondelete="SET NULL")),
        sa.Column("is_reserve", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_admin_assignment_chat_user"),
    )
    op.create_index("ix_admin_assignments_chat_id", "admin_assignments", ["chat_id"])
    op.create_index("ix_admin_assignments_user_id", "admin_assignments", ["user_id"])
    op.create_index("ix_admin_assignments_role_id", "admin_assignments", ["role_id"])

    op.create_table(
        "wallets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_wallet_chat_user"),
    )
    op.create_index("ix_wallets_chat_id", "wallets", ["chat_id"])
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])

    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("transaction_id", sa.String(36), nullable=False, unique=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="SET NULL")),
        sa.Column("counterparty_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="SET NULL")),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_transactions_chat_id", "transactions", ["chat_id"])
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"])
    op.create_index("ix_transactions_kind", "transactions", ["kind"])
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="SET NULL")),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="SET NULL")),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64)),
        sa.Column("target_id", sa.String(128)),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_audit_log_chat_id", "audit_log", ["chat_id"])
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("processed_updates")
    op.drop_table("audit_log")
    op.drop_table("transactions")
    op.drop_table("wallets")
    op.drop_table("admin_assignments")
    op.drop_table("admin_permissions")
    op.drop_table("admin_roles")
    op.drop_table("group_settings")
    op.drop_table("group_members")
    op.drop_table("group_owners")
    op.drop_table("groups")
    op.drop_table("users")
