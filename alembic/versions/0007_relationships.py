"""add relationships

Revision ID: 0007_relationships
Revises: 0006_rp_core
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0007_relationships"
down_revision: str | None = "0006_rp_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relationships",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user1_id", sa.BigInteger(), nullable=False),
        sa.Column("user2_id", sa.BigInteger(), nullable=False),
        sa.Column("proposer_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("chat_id", "user1_id", "user2_id", name="uq_relationship_pair"),
    )
    op.create_index("ix_relationships_chat_status", "relationships", ["chat_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_relationships_chat_status", table_name="relationships")
    op.drop_table("relationships")
