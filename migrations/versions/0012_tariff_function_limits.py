"""finalize Mimorus tariff function limits

Revision ID: 0012_tariff_function_limits
Revises: 0011_tariff_catalog_v1
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_tariff_function_limits"
down_revision: str | None = "0011_tariff_catalog_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LIMITS: dict[str, dict[str, int | None]] = {
    "TEST": {
        "main_groups": 1,
        "network_test_groups": 1,
        "networks": 1,
        "network_groups_per_network": 2,
        "blocked_word_lists": 1,
        "blocked_words": 3,
        "blocked_phrase_lists": 1,
        "blocked_phrases": 3,
        "custom_reasons": 3,
        "admin_ranks": 3,
        "reserve_admins": 1,
        "exports": 10,
        "protection_schedules": 1,
        "automatic_roles": 3,
        "log_groups": 1,
        "custom_achievements": 1,
        "auto_messages": 1,
        "templates": 1,
        "auto_repeats": 1,
    },
    "BASIC": {
        "main_groups": 3,
        "networks": 1,
        "network_groups_per_network": 3,
        "blocked_word_lists": 3,
        "blocked_words": 50,
        "blocked_phrase_lists": 3,
        "blocked_phrases": 30,
        "custom_reasons": 10,
        "admin_ranks": 5,
        "reserve_admins": 1,
        "exports": 100,
        "protection_schedules": 1,
        "automatic_roles": 5,
        "log_groups": 1,
        "custom_achievements": 5,
        "auto_messages": 3,
        "templates": 5,
        "auto_repeats": 3,
    },
    "STANDARD": {
        "main_groups": 10,
        "networks": 3,
        "network_groups_per_network": 10,
        "blocked_word_lists": 10,
        "blocked_words": 200,
        "blocked_phrase_lists": 10,
        "blocked_phrases": 100,
        "custom_reasons": 30,
        "admin_ranks": 10,
        "reserve_admins": 2,
        "exports": 500,
        "protection_schedules": 3,
        "automatic_roles": 15,
        "log_groups": 3,
        "custom_achievements": 20,
        "auto_messages": 10,
        "templates": 20,
        "auto_repeats": 10,
    },
    "PRO": {
        "main_groups": 30,
        "networks": 10,
        "network_groups_per_network": 30,
        "blocked_word_lists": 30,
        "blocked_words": 500,
        "blocked_phrase_lists": 30,
        "blocked_phrases": 300,
        "custom_reasons": 100,
        "admin_ranks": 25,
        "reserve_admins": 5,
        "exports": 2000,
        "protection_schedules": 10,
        "automatic_roles": 50,
        "log_groups": 10,
        "custom_achievements": 100,
        "auto_messages": 30,
        "templates": 100,
        "auto_repeats": 30,
    },
    "MAX": {
        "main_groups": 100,
        "networks": 25,
        "network_groups_per_network": 100,
        "blocked_word_lists": 100,
        "blocked_words": 2000,
        "blocked_phrase_lists": 100,
        "blocked_phrases": 1000,
        "custom_reasons": 300,
        "admin_ranks": 50,
        "reserve_admins": 10,
        "exports": None,
        "protection_schedules": 25,
        "automatic_roles": 150,
        "log_groups": 25,
        "custom_achievements": 300,
        "auto_messages": 100,
        "templates": 300,
        "auto_repeats": 100,
    },
}

MAX_GROUPS = {
    "TEST": 1,
    "BASIC": 3,
    "STANDARD": 10,
    "PRO": 30,
    "MAX": 100,
}


def upgrade() -> None:
    conn = op.get_bind()
    for code, configured in LIMITS.items():
        row = conn.execute(
            sa.text("SELECT limits_json FROM tariffs WHERE code = :code"),
            {"code": code},
        ).mappings().first()
        if row is None:
            continue

        limits = dict(row["limits_json"] or {})
        # VIP is intentionally unlimited and is no longer represented by a
        # tariff quantity. RP/custom content will receive its own key later.
        limits.pop("custom_vip_rp", None)
        if code != "TEST":
            limits.pop("network_test_groups", None)

        for key, value in configured.items():
            if value is None:
                limits.pop(key, None)
            else:
                limits[key] = value

        conn.execute(
            sa.text(
                "UPDATE tariffs SET max_groups = :max_groups, "
                "limits_json = CAST(:limits_json AS json), updated_at = now() "
                "WHERE code = :code"
            ),
            {
                "code": code,
                "max_groups": MAX_GROUPS[code],
                "limits_json": json.dumps(limits),
            },
        )


def downgrade() -> None:
    # These are product configuration values. Historical creator-edited
    # limits cannot be reconstructed safely, so downgrade leaves data intact.
    pass
