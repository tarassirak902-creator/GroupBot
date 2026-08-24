from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from groupbot.models import Base


class Network(Base):
    __tablename__ = "networks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class NetworkGroup(Base):
    __tablename__ = "network_groups"
    __table_args__ = (
        UniqueConstraint("network_id", "chat_id", name="uq_network_group_network_chat"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    network_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("networks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.chat_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
