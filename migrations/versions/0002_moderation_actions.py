"""persistent moderation actions

Revision ID: 0002_moderation_actions
Revises: 0001_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_moderation_actions"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moderation_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("warning_index", sa.Integer()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_moderation_actions_chat_id", "moderation_actions", ["chat_id"])
    op.create_index("ix_moderation_actions_target_user_id", "moderation_actions", ["target_user_id"])
    op.create_index("ix_moderation_actions_actor_user_id", "moderation_actions", ["actor_user_id"])
    op.create_index("ix_moderation_actions_action", "moderation_actions", ["action"])
    op.create_index("ix_moderation_actions_active", "moderation_actions", ["chat_id", "action", "is_active"])
    op.create_index("ix_moderation_actions_created_at", "moderation_actions", ["created_at"])


def downgrade() -> None:
    op.drop_table("moderation_actions")
