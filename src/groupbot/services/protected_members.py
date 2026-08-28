from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole
from groupbot.services.permissions import is_group_owner
from groupbot.services.special_statuses import is_active_group_member, special_status_ids


HELPER_ROLE = "Помощник"


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

    admin_role = (
        await session.execute(
            select(AdminRole.name)
            .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == user_id,
                AdminRole.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if admin_role is not None and admin_role != HELPER_ROLE:
        return True

    # VIP/Nedotriga are participant statuses, not permanent user flags. A stale
    # ID left in historical JSON after the user left the group must not grant
    # immunity after a later rejoin.
    if not await is_active_group_member(session, chat_id=chat_id, user_id=user_id):
        return False
    return (
        user_id in special_status_ids(moderation_config, "vip")
        or user_id in special_status_ids(moderation_config, "nedotroga")
    )
