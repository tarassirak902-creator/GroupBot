from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.moderation_models import ObservedMessage


AUTO_MODERATION_CLAIM_KEY = "_mimorus_auto_moderation_claim"


def automatic_moderation_claimed(data: dict[str, Any]) -> bool:
    """Return True when another automatic protection already owns this update."""
    return bool(data.get(AUTO_MODERATION_CLAIM_KEY))


def claim_automatic_moderation(data: dict[str, Any], source: str) -> bool:
    """Atomically claim one dispatcher update for exactly one automatic protection.

    Aiogram passes the same data mapping through the outer middleware chain, so the
    first matching protection wins and downstream protections must not punish the
    same Telegram message again.
    """
    if automatic_moderation_claimed(data):
        return False
    data[AUTO_MODERATION_CLAIM_KEY] = source
    return True


async def mark_observed_deleted(
    session: AsyncSession,
    *,
    chat_id: int,
    message_ids: list[int] | tuple[int, ...] | set[int],
    deleted_at,
) -> None:
    ids = [int(value) for value in message_ids]
    if not ids:
        return
    await session.execute(
        update(ObservedMessage)
        .where(
            ObservedMessage.chat_id == chat_id,
            ObservedMessage.message_id.in_(ids),
            ObservedMessage.deleted_at.is_(None),
        )
        .values(deleted_at=deleted_at)
    )
