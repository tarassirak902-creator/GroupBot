"""manual advertising invite links

Revision ID: 0019_manual_ad_links
Revises: 0018_manual_ads
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
revision: str = "0019_manual_ad_links"
down_revision: str | None = "0018_manual_ads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "advertising_manual_links",
        sa.Column("id",sa.BigInteger(),autoincrement=True,nullable=False),
        sa.Column("target_chat_id",sa.BigInteger(),nullable=False),
        sa.Column("owner_user_id",sa.BigInteger(),nullable=False),
        sa.Column("invite_url",sa.String(512),nullable=False),
        sa.Column("target_title",sa.String(255),nullable=False),
        sa.Column("mode",sa.String(24),nullable=False),
        sa.Column("quantity",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["target_chat_id"],["groups.chat_id"],ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"],["users.telegram_user_id"],ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),sa.UniqueConstraint("invite_url",name="uq_manual_ad_link_url"),
    )
    op.create_index("ix_advertising_manual_links_target_chat_id","advertising_manual_links",["target_chat_id"])

def downgrade() -> None:
    op.drop_table("advertising_manual_links")
