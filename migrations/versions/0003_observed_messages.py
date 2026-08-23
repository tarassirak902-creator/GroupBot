"""observed messages for moderation cleanup

Revision ID: 0003_observed_messages
Revises: 0002_moderation_actions, 0002_tariffs_subscriptions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_observed_messages"
down_revision: tuple[str, str] = ("0002_moderation_actions", "0002_tariffs_subscriptions")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observed_messages",
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_observed_messages_chat_user", "observed_messages", ["chat_id", "user_id"])
    op.create_index("ix_observed_messages_sent_at", "observed_messages", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_observed_messages_sent_at", table_name="observed_messages")
    op.drop_index("ix_observed_messages_chat_user", table_name="observed_messages")
    op.drop_table("observed_messages")
