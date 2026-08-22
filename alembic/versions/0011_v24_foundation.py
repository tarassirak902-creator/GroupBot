"""add v2.4 ownership permissions wallets and audit foundation

Revision ID: 0011_v24_foundation
Revises: 0010_mod_warn_white
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_v24_foundation"
down_revision: str | None = "0010_mod_warn_white"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_owners",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_group_owners_chat_id", "group_owners", ["chat_id"], unique=False)
    op.create_index("ix_group_owners_user_id", "group_owners", ["user_id"], unique=False)
    op.create_index(
        "uq_group_owners_active_chat",
        "group_owners",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "admin_roles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("is_reserved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "name", name="uq_admin_roles_chat_name"),
    )
    op.create_index("ix_admin_roles_chat_id", "admin_roles", ["chat_id"], unique=False)

    op.create_table(
        "admin_permissions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("permission", sa.String(length=128), nullable=False),
        sa.Column("is_allowed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["admin_roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_id", "permission", name="uq_admin_permission_role_key"),
    )
    op.create_index("ix_admin_permissions_role_id", "admin_permissions", ["role_id"], unique=False)

    op.create_table(
        "admin_assignments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("assigned_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["admin_roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "user_id", "role_id", name="uq_admin_assignment"),
    )
    op.create_index("ix_admin_assignments_chat_id", "admin_assignments", ["chat_id"], unique=False)
    op.create_index("ix_admin_assignments_user_id", "admin_assignments", ["user_id"], unique=False)
    op.create_index("ix_admin_assignments_role_id", "admin_assignments", ["role_id"], unique=False)

    op.create_table(
        "wallets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_wallet_chat_user"),
    )
    op.create_index("ix_wallets_chat_id", "wallets", ["chat_id"], unique=False)
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"], unique=False)
    op.execute(sa.text("""
        INSERT INTO wallets(chat_id, user_id, balance)
        SELECT chat_id, user_id, balance FROM group_users
        ON CONFLICT (chat_id, user_id) DO NOTHING
    """))

    op.add_column("transactions", sa.Column("transaction_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_transactions_transaction_id", "transactions", ["transaction_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_chat_id", "audit_log", ["chat_id"], unique=False)
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"], unique=False)
    op.create_index("ix_audit_log_target_user_id", "audit_log", ["target_user_id"], unique=False)
    op.create_index("ix_audit_log_action", "audit_log", ["action"], unique=False)
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_target_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_chat_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_constraint("uq_transactions_transaction_id", "transactions", type_="unique")
    op.drop_column("transactions", "transaction_id")

    op.drop_index("ix_wallets_user_id", table_name="wallets")
    op.drop_index("ix_wallets_chat_id", table_name="wallets")
    op.drop_table("wallets")

    op.drop_index("ix_admin_assignments_role_id", table_name="admin_assignments")
    op.drop_index("ix_admin_assignments_user_id", table_name="admin_assignments")
    op.drop_index("ix_admin_assignments_chat_id", table_name="admin_assignments")
    op.drop_table("admin_assignments")

    op.drop_index("ix_admin_permissions_role_id", table_name="admin_permissions")
    op.drop_table("admin_permissions")

    op.drop_index("ix_admin_roles_chat_id", table_name="admin_roles")
    op.drop_table("admin_roles")

    op.drop_index("uq_group_owners_active_chat", table_name="group_owners")
    op.drop_index("ix_group_owners_user_id", table_name="group_owners")
    op.drop_index("ix_group_owners_chat_id", table_name="group_owners")
    op.drop_table("group_owners")
