"""support tickets

Revision ID: 0020_support_tickets
Revises: 0019_manual_ad_links
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_support_tickets"
down_revision: str | None = "0019_manual_ad_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("source_label", sa.String(255), nullable=False, server_default="ЛС с ботом"),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_chat_id"], ["groups.chat_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"])
    op.create_index("ix_support_tickets_source_chat_id", "support_tickets", ["source_chat_id"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
    op.create_index("ix_support_tickets_deleted_at", "support_tickets", ["deleted_at"])
    op.create_table(
        "support_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_user_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_role", sa.String(24), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_messages_ticket_id", "support_messages", ["ticket_id"])
    op.create_index("ix_support_messages_sender_user_id", "support_messages", ["sender_user_id"])


def downgrade() -> None:
    op.drop_table("support_messages")
    op.drop_table("support_tickets")
