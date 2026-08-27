from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from groupbot.models import Base


class TelegramAdminPromotion(Base):
    __tablename__ = "telegram_admin_promotions"

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.chat_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
