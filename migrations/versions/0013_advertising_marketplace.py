"""add advertising marketplace tables

Revision ID: 0013_advertising_marketplace
Revises: 0012_tariff_function_limits
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_advertising_marketplace"
down_revision: str | None = "0012_tariff_function_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "advertising_listings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_title_snapshot", sa.String(255), nullable=False),
        sa.Column("member_count_snapshot", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("offers_post", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("offers_mandatory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("post_price_stars", sa.Integer()),
        sa.Column("post_interval_minutes", sa.Integer()),
        sa.Column("post_terms_json", sa.JSON()),
        sa.Column("mandatory_price_stars", sa.Integer()),
        sa.Column("mandatory_terms_json", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("chat_id", name="uq_advertising_listing_chat"),
        sa.CheckConstraint("offers_post OR offers_mandatory", name="ck_advertising_listing_has_offer"),
    )
    op.create_index("ix_advertising_listings_owner_user_id", "advertising_listings", ["owner_user_id"])
    op.create_index("ix_advertising_listings_chat_id", "advertising_listings", ["chat_id"])
    op.create_index("ix_advertising_listings_is_active", "advertising_listings", ["is_active"])

    op.create_table(
        "advertising_deals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("listing_id", sa.BigInteger(), sa.ForeignKey("advertising_listings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("seller_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("buyer_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("requested_post", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_mandatory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(48), nullable=False, server_default="pending"),
        sa.Column("buyer_message", sa.Text()),
        sa.Column("agreed_terms_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("first_no_claims_at", sa.DateTime(timezone=True)),
        sa.Column("no_claims_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    for column in ["listing_id", "seller_user_id", "buyer_user_id", "status", "created_at", "accepted_at", "started_at", "finished_at", "first_no_claims_at", "no_claims_deadline_at", "completed_at"]:
        op.create_index(f"ix_advertising_deals_{column}", "advertising_deals", [column])

    op.create_table(
        "advertising_placements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.BigInteger(), sa.ForeignKey("advertising_deals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("config_json", sa.JSON()),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("last_published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("deal_id", "kind", name="uq_advertising_placement_deal_kind"),
    )
    for column in ["deal_id", "kind", "status", "starts_at", "ends_at"]:
        op.create_index(f"ix_advertising_placements_{column}", "advertising_placements", [column])

    op.create_table(
        "advertising_no_claims_confirmations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.BigInteger(), sa.ForeignKey("advertising_deals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("deal_id", "user_id", name="uq_advertising_no_claims_deal_user"),
    )
    op.create_index("ix_advertising_no_claims_confirmations_deal_id", "advertising_no_claims_confirmations", ["deal_id"])
    op.create_index("ix_advertising_no_claims_confirmations_user_id", "advertising_no_claims_confirmations", ["user_id"])

    op.create_table(
        "advertising_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.BigInteger(), sa.ForeignKey("advertising_deals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reviewed_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("deal_id", "reviewer_user_id", name="uq_advertising_review_deal_reviewer"),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_advertising_review_rating"),
    )
    op.create_index("ix_advertising_reviews_deal_id", "advertising_reviews", ["deal_id"])
    op.create_index("ix_advertising_reviews_reviewer_user_id", "advertising_reviews", ["reviewer_user_id"])
    op.create_index("ix_advertising_reviews_reviewed_user_id", "advertising_reviews", ["reviewed_user_id"])

    op.create_table(
        "advertising_disputes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.BigInteger(), sa.ForeignKey("advertising_deals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opened_by_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("reason", sa.String(255)),
        sa.Column("description", sa.Text()),
        sa.Column("resolution", sa.Text()),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id", ondelete="SET NULL")),
        sa.UniqueConstraint("deal_id", name="uq_advertising_dispute_deal"),
    )
    op.create_index("ix_advertising_disputes_deal_id", "advertising_disputes", ["deal_id"])
    op.create_index("ix_advertising_disputes_opened_by_user_id", "advertising_disputes", ["opened_by_user_id"])
    op.create_index("ix_advertising_disputes_status", "advertising_disputes", ["status"])
    op.create_index("ix_advertising_disputes_resolved_by_user_id", "advertising_disputes", ["resolved_by_user_id"])


def downgrade() -> None:
    op.drop_table("advertising_disputes")
    op.drop_table("advertising_reviews")
    op.drop_table("advertising_no_claims_confirmations")
    op.drop_table("advertising_placements")
    op.drop_table("advertising_deals")
    op.drop_table("advertising_listings")
