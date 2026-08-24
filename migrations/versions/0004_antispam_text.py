"""store normalized message text for antispam

Revision ID: 0004_antispam_text
Revises: 0003_observed_messages
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_antispam_text"
down_revision: str | None = "0003_observed_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("observed_messages", sa.Column("normalized_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("observed_messages", "normalized_text")
