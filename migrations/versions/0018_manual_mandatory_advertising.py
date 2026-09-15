"""manual mandatory advertising

Revision ID: 0018_manual_mandatory_advertising
Revises: 0017_persistent_entry_protection
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
revision: str = "0018_manual_mandatory_advertising"
down_revision: str | None = "0017_persistent_entry_protection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table("advertising_manual_ops",sa.Column("id",sa.BigInteger(),autoincrement=True,nullable=False),sa.Column("source_chat_id",sa.BigInteger(),nullable=False),sa.Column("owner_user_id",sa.BigInteger(),nullable=False),sa.Column("target_chat_id",sa.BigInteger(),nullable=True),sa.Column("target_url",sa.String(512),nullable=False),sa.Column("target_title",sa.String(255),nullable=False),sa.Column("mode",sa.String(24),nullable=False),sa.Column("quantity",sa.Integer(),nullable=False),sa.Column("progress_count",sa.Integer(),server_default="0",nullable=False),sa.Column("status",sa.String(24),server_default="active",nullable=False),sa.Column("starts_at",sa.DateTime(timezone=True),server_default=sa.text("now()"),nullable=False),sa.Column("ends_at",sa.DateTime(timezone=True),nullable=True),sa.Column("completed_at",sa.DateTime(timezone=True),nullable=True),sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.text("now()"),nullable=False),sa.ForeignKeyConstraint(["source_chat_id"],["groups.chat_id"],ondelete="CASCADE"),sa.ForeignKeyConstraint(["owner_user_id"],["users.telegram_user_id"],ondelete="CASCADE"),sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_advertising_manual_ops_source_chat_id","advertising_manual_ops",["source_chat_id"]);op.create_index("ix_advertising_manual_ops_target_chat_id","advertising_manual_ops",["target_chat_id"]);op.create_index("ix_advertising_manual_ops_status","advertising_manual_ops",["status"]);op.create_index("ix_advertising_manual_ops_ends_at","advertising_manual_ops",["ends_at"])
    op.create_table("advertising_manual_op_credits",sa.Column("id",sa.BigInteger(),autoincrement=True,nullable=False),sa.Column("op_id",sa.BigInteger(),nullable=False),sa.Column("user_id",sa.BigInteger(),nullable=False),sa.Column("satisfied",sa.Boolean(),server_default=sa.text("true"),nullable=False),sa.Column("counted",sa.Boolean(),server_default=sa.text("false"),nullable=False),sa.Column("reason",sa.String(32),nullable=False),sa.Column("credited_at",sa.DateTime(timezone=True),server_default=sa.text("now()"),nullable=False),sa.ForeignKeyConstraint(["op_id"],["advertising_manual_ops.id"],ondelete="CASCADE"),sa.PrimaryKeyConstraint("id"),sa.UniqueConstraint("op_id","user_id",name="uq_manual_op_credit_user"))
    op.create_index("ix_advertising_manual_op_credits_op_id","advertising_manual_op_credits",["op_id"]);op.create_index("ix_advertising_manual_op_credits_user_id","advertising_manual_op_credits",["user_id"])

def downgrade() -> None:
    op.drop_table("advertising_manual_op_credits");op.drop_table("advertising_manual_ops")
