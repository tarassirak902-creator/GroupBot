"""track Telegram admins promoted by Mimorus

Revision ID: 0015_telegram_admin_promotions
Revises: 0014_mutual_op
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_telegram_admin_promotions"
down_revision: str | None = "0014_mutual_op"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_admin_promotions",
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.chat_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "promoted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("telegram_admin_promotions")
