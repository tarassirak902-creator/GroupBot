"""persist captcha and anti-raid runtime state

Revision ID: 0017_persistent_entry_protection
Revises: 0016_admin_assignment_actor
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_persistent_entry_protection"
down_revision: str | None = "0016_admin_assignment_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entry_captcha_challenges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("expected_answer", sa.String(length=64), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fail_action", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_entry_captcha_chat_user"),
    )
    op.create_index("ix_entry_captcha_challenges_chat_id", "entry_captcha_challenges", ["chat_id"], unique=False)
    op.create_index("ix_entry_captcha_challenges_user_id", "entry_captcha_challenges", ["user_id"], unique=False)
    op.create_index("ix_entry_captcha_challenges_deadline_at", "entry_captcha_challenges", ["deadline_at"], unique=False)

    op.create_table(
        "entry_raid_states",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_index("ix_entry_raid_states_active_until", "entry_raid_states", ["active_until"], unique=False)

    op.create_table(
        "entry_join_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entry_join_events_chat_id", "entry_join_events", ["chat_id"], unique=False)
    op.create_index("ix_entry_join_events_user_id", "entry_join_events", ["user_id"], unique=False)
    op.create_index("ix_entry_join_events_joined_at", "entry_join_events", ["joined_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_entry_join_events_joined_at", table_name="entry_join_events")
    op.drop_index("ix_entry_join_events_user_id", table_name="entry_join_events")
    op.drop_index("ix_entry_join_events_chat_id", table_name="entry_join_events")
    op.drop_table("entry_join_events")

    op.drop_index("ix_entry_raid_states_active_until", table_name="entry_raid_states")
    op.drop_table("entry_raid_states")

    op.drop_index("ix_entry_captcha_challenges_deadline_at", table_name="entry_captcha_challenges")
    op.drop_index("ix_entry_captcha_challenges_user_id", table_name="entry_captcha_challenges")
    op.drop_index("ix_entry_captcha_challenges_chat_id", table_name="entry_captcha_challenges")
    op.drop_table("entry_captcha_challenges")
