from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from groupbot.models import Base


class AdvertisingListing(Base):
    __tablename__ = "advertising_listings"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_advertising_listing_chat"),
        CheckConstraint("offers_post OR offers_mandatory", name="ck_advertising_listing_has_offer"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.chat_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    member_count_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    offers_post: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    offers_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    post_price_stars: Mapped[int | None] = mapped_column(Integer)
    post_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    post_terms_json: Mapped[dict | None] = mapped_column(JSON)
    mandatory_price_stars: Mapped[int | None] = mapped_column(Integer)
    mandatory_terms_json: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AdvertisingDeal(Base):
    __tablename__ = "advertising_deals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_listings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    seller_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    buyer_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    requested_post: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    requested_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="pending", server_default="pending", index=True)
    buyer_message: Mapped[str | None] = mapped_column(Text)
    agreed_terms_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_no_claims_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    no_claims_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AdvertisingPlacement(Base):
    __tablename__ = "advertising_placements"
    __table_args__ = (
        UniqueConstraint("deal_id", "kind", name="uq_advertising_placement_deal_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    config_json: Mapped[dict | None] = mapped_column(JSON)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertisingNoClaimsConfirmation(Base):
    __tablename__ = "advertising_no_claims_confirmations"
    __table_args__ = (
        UniqueConstraint("deal_id", "user_id", name="uq_advertising_no_claims_deal_user"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertisingReview(Base):
    __tablename__ = "advertising_reviews"
    __table_args__ = (
        UniqueConstraint("deal_id", "reviewer_user_id", name="uq_advertising_review_deal_reviewer"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_advertising_review_rating"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reviewed_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertisingDispute(Base):
    __tablename__ = "advertising_disputes"
    __table_args__ = (
        UniqueConstraint("deal_id", name="uq_advertising_dispute_deal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opened_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", server_default="open", index=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="SET NULL"),
        index=True,
    )
