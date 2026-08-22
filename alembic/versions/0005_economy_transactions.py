"""add economy transactions

Revision ID: 0005_economy_transactions
Revises: 0004_achievements
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_economy_transactions"
down_revision: str | None = "0004_achievements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("from_user_id", sa.BigInteger(), nullable=True),
        sa.Column("to_user_id", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_chat_created", "transactions", ["chat_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transactions_chat_created", table_name="transactions")
    op.drop_table("transactions")
