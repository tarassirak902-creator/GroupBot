"""add auto activity

Revision ID: 0008_auto_activity
Revises: 0007_relationships
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0008_auto_activity"
down_revision: str | None = "0007_relationships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auto_event_settings",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("min_interval_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("max_interval_minutes", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("activity_window_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_template_key", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "activity_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("template_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_activity_events_chat_created", "activity_events", ["chat_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_activity_events_chat_created", table_name="activity_events")
    op.drop_table("activity_events")
    op.drop_table("auto_event_settings")
