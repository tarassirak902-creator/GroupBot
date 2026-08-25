from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from groupbot.models import Base


class TelegramStarsPayment(Base):
    __tablename__ = "telegram_stars_payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(255))
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    tariff_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tariffs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        index=True,
    )
    invoice_payload: Mapped[str] = mapped_column(String(255), nullable=False)
    stars_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="XTR", server_default="XTR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
