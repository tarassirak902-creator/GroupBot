"""add network administrators

Revision ID: 0006_network_admins
Revises: 0005_reset_warnings_on_unban
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_network_admins"
down_revision: str | None = "0005_reset_warnings_on_unban"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "network_admins",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("permissions_json", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", "user_id", name="uq_network_admin_owner_user"),
    )
    op.create_index("ix_network_admins_owner_user_id", "network_admins", ["owner_user_id"], unique=False)
    op.create_index("ix_network_admins_user_id", "network_admins", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_network_admins_user_id", table_name="network_admins")
    op.drop_index("ix_network_admins_owner_user_id", table_name="network_admins")
    op.drop_table("network_admins")
