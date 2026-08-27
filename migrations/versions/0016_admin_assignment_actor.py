"""track who assigned the current admin rank

Revision ID: 0016_admin_assignment_actor
Revises: 0015_telegram_admin_promotions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_admin_assignment_actor"
down_revision: str | None = "0015_telegram_admin_promotions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "admin_assignments",
        sa.Column("assigned_by_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_admin_assignments_assigned_by_user",
        "admin_assignments",
        "users",
        ["assigned_by_user_id"],
        ["telegram_user_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_admin_assignments_assigned_by_user_id",
        "admin_assignments",
        ["assigned_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_assignments_assigned_by_user_id", table_name="admin_assignments")
    op.drop_constraint("fk_admin_assignments_assigned_by_user", "admin_assignments", type_="foreignkey")
    op.drop_column("admin_assignments", "assigned_by_user_id")
