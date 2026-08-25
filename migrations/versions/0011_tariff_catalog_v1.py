"""set initial Mimorus tariff catalog

Revision ID: 0011_tariff_catalog_v1
Revises: 0010_telegram_stars_payments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_tariff_catalog_v1"
down_revision: str | None = "0010_telegram_stars_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CATALOG = {
    "TEST": {"duration_days": 3, "max_members": 200000, "max_groups": 1, "stars": None},
    "BASIC": {"duration_days": 30, "max_members": 25000, "max_groups": 1, "stars": 299},
    "STANDARD": {"duration_days": 30, "max_members": 75000, "max_groups": 3, "stars": 599},
    "PRO": {"duration_days": 30, "max_members": 150000, "max_groups": 10, "stars": 999},
    "MAX": {"duration_days": 30, "max_members": 200000, "max_groups": 25, "stars": 1499},
}


def upgrade() -> None:
    conn = op.get_bind()
    for code, values in CATALOG.items():
        row = conn.execute(
            sa.text("SELECT limits_json FROM tariffs WHERE code = :code"),
            {"code": code},
        ).mappings().first()
        if row is None:
            continue
        limits = dict(row["limits_json"] or {})
        if values["stars"] is None:
            limits.pop("stars_price", None)
        else:
            limits["stars_price"] = values["stars"]
        conn.execute(
            sa.text(
                "UPDATE tariffs "
                "SET duration_days = :duration_days, "
                "max_members_per_group = :max_members, "
                "max_groups = :max_groups, "
                "limits_json = CAST(:limits_json AS json), "
                "updated_at = now() "
                "WHERE code = :code"
            ),
            {
                "code": code,
                "duration_days": values["duration_days"],
                "max_members": values["max_members"],
                "max_groups": values["max_groups"],
                "limits_json": __import__("json").dumps(limits),
            },
        )


def downgrade() -> None:
    # Catalog values are product configuration. Do not guess historical
    # creator-edited values during downgrade.
    pass
