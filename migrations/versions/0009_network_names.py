"""add network names

Revision ID: 0009_network_names
Revises: 0008_subscription_addons
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_network_names"
down_revision: str | None = "0008_subscription_addons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("networks", sa.Column("name", sa.String(length=100), nullable=True))
    op.execute("UPDATE networks SET name = 'Сетка ' || id::text WHERE name IS NULL")
    op.alter_column("networks", "name", nullable=False)


def downgrade() -> None:
    op.drop_column("networks", "name")
