"""add achievements

Revision ID: 0004_achievements
Revises: 0003_xp_config
Create Date: 2026-08-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_achievements"
down_revision: str | None = "0003_xp_config"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "achievements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("condition_type", sa.String(length=64), nullable=False),
        sa.Column("condition_value", sa.Integer(), nullable=False),
        sa.Column("reward_xp", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reward_currency", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "code", name="uq_achievements_chat_code"),
    )
    op.create_index("ix_achievements_chat_id", "achievements", ["chat_id"])

    op.create_table(
        "user_achievements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("achievement_id", sa.BigInteger(), nullable=False),
        sa.Column("awarded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["groups.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["achievement_id"], ["achievements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "user_id", "achievement_id", name="uq_user_achievement_once"),
    )
    op.create_index("ix_user_achievements_chat_id", "user_achievements", ["chat_id"])
    op.create_index("ix_user_achievements_user_id", "user_achievements", ["user_id"])
    op.create_index("ix_user_achievements_achievement_id", "user_achievements", ["achievement_id"])


def downgrade() -> None:
    op.drop_index("ix_user_achievements_achievement_id", table_name="user_achievements")
    op.drop_index("ix_user_achievements_user_id", table_name="user_achievements")
    op.drop_index("ix_user_achievements_chat_id", table_name="user_achievements")
    op.drop_table("user_achievements")
    op.drop_index("ix_achievements_chat_id", table_name="achievements")
    op.drop_table("achievements")
