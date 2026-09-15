from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from groupbot.models import Base


class AdvertisingManualOp(Base):
    __tablename__ = "advertising_manual_ops"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False, index=True)
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    target_url: Mapped[str] = mapped_column(String(512), nullable=False)
    target_title: Mapped[str] = mapped_column(String(255), nullable=False, default="Рекламная группа")
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    progress_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active", index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertisingManualOpCredit(Base):
    __tablename__ = "advertising_manual_op_credits"
    __table_args__ = (UniqueConstraint("op_id", "user_id", name="uq_manual_op_credit_user"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    op_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("advertising_manual_ops.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    credited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertisingManualLink(Base):
    __tablename__ = "advertising_manual_links"
    __table_args__ = (UniqueConstraint("invite_url", name="uq_manual_ad_link_url"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    target_chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False)
    invite_url: Mapped[str] = mapped_column(String(512), nullable=False)
    target_title: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
