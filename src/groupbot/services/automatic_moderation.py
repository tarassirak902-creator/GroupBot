from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import GroupMember
from groupbot.moderation_models import ObservedMessage


AUTO_MODERATION_CLAIM_KEY = "_mimorus_auto_moderation_claim"


def automatic_moderation_claimed(data: dict[str, Any]) -> bool:
    """Return True when another automatic protection already owns this update."""
    return bool(data.get(AUTO_MODERATION_CLAIM_KEY))


def claim_automatic_moderation(data: dict[str, Any], source: str) -> bool:
    """Claim punishment ownership for one dispatcher update.

    Cleanup filters may still delete matching content after a claim. Only the
    punitive action is exclusive, preventing one Telegram message from generating
    multiple warnings/mutes/bans while keeping the chat clean.
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
) -> int:
    """Mark newly deleted messages and update per-member counters exactly once."""
    ids = [int(value) for value in message_ids]
    if not ids:
        return 0

    result = await session.execute(
        update(ObservedMessage)
        .where(
            ObservedMessage.chat_id == chat_id,
            ObservedMessage.message_id.in_(ids),
            ObservedMessage.deleted_at.is_(None),
        )
        .values(deleted_at=deleted_at)
        .returning(ObservedMessage.user_id)
    )
    affected_user_ids = [int(value) for value in result.scalars().all()]
    if not affected_user_ids:
        return 0

    for user_id, count in Counter(affected_user_ids).items():
        await session.execute(
            update(GroupMember)
            .where(
                GroupMember.chat_id == chat_id,
                GroupMember.user_id == user_id,
            )
            .values(deleted_messages=GroupMember.deleted_messages + count)
        )
    return len(affected_user_ids)
