"""add owner networks and network groups

Revision ID: 0007_networks
Revises: 0006_network_admins
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_networks"
down_revision: str | None = "0006_network_admins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "networks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_networks_owner_user_id", "networks", ["owner_user_id"], unique=False)

    op.create_table(
        "network_groups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("network_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["network_id"], ["networks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network_id", "chat_id", name="uq_network_group_network_chat"),
    )
    op.create_index("ix_network_groups_network_id", "network_groups", ["network_id"], unique=False)
    op.create_index("ix_network_groups_chat_id", "network_groups", ["chat_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_network_groups_chat_id", table_name="network_groups")
    op.drop_index("ix_network_groups_network_id", table_name="network_groups")
    op.drop_table("network_groups")
    op.drop_index("ix_networks_owner_user_id", table_name="networks")
    op.drop_table("networks")
