from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from groupbot.models import Base


class AdvertisingMutualOpDirection(Base):
    __tablename__ = "advertising_mutual_op_directions"
    __table_args__ = (
        UniqueConstraint("deal_id", "source_chat_id", name="uq_mutual_op_deal_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.chat_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.chat_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_title: Mapped[str] = mapped_column(String(255), nullable=False)
    target_title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    invite_link: Mapped[str | None] = mapped_column(String(512))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertisingMutualOpMember(Base):
    __tablename__ = "advertising_mutual_op_members"
    __table_args__ = (
        UniqueConstraint("direction_id", "user_id", name="uq_mutual_op_direction_user"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    direction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advertising_mutual_op_directions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true", index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
