from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment
from groupbot.services.permissions import is_group_owner


async def is_protected_member(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    moderation_config: dict | None,
) -> bool:
    """Return True for roles that automatic moderation must never punish."""
    if await is_group_owner(session, chat_id, user_id):
        return True

    admin_assignment = (
        await session.execute(
            select(AdminAssignment.id).where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if admin_assignment is not None:
        return True

    special = dict((moderation_config or {}).get("special_statuses") or {})
    protected_ids = {
        int(value)
        for value in [*(special.get("vip") or []), *(special.get("nedotroga") or [])]
        if str(value).lstrip("-").isdigit()
    }
    return user_id in protected_ids
