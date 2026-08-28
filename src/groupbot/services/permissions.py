from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminPermission, AdminRole, GroupOwner


OWNER_PERMISSION = "*"
HELPER_ROLE = "Помощник"
HELPER_BLOCKED_PERMISSIONS = {"warning", "mute", "ban", "unmute", "unban", "delete", "pin", "punishment_lists", "stats"}


async def is_group_owner(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    return (await session.execute(
        select(GroupOwner.id).where(
            GroupOwner.chat_id == chat_id,
            GroupOwner.user_id == user_id,
            GroupOwner.is_current.is_(True),
        )
    )).scalar_one_or_none() is not None


async def has_permission(session: AsyncSession, chat_id: int, user_id: int, permission: str) -> bool:
    """Check local Mimorus permissions for one concrete group.

    Network-admin permissions are intentionally excluded here. They authorize
    only the dedicated network commands (сбан/сразбан/сбанлист), whose router
    performs its own network-scoped permission check. Mixing the two scopes
    would allow a network permission such as ``ban`` or historical ``*`` to
    grant ordinary local moderation rights.
    """
    if await is_group_owner(session, chat_id, user_id):
        return True

    role = (await session.execute(
        select(AdminRole)
        .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
        .where(
            AdminAssignment.chat_id == chat_id,
            AdminAssignment.user_id == user_id,
            AdminRole.is_active.is_(True),
        )
    )).scalar_one_or_none()
    if role is None:
        return False

    # Helper is a reporter attached to an administrator, not a moderator.
    # Historical/accidental permission rows must never grant moderation access.
    if role.name == HELPER_ROLE and permission in HELPER_BLOCKED_PERMISSIONS:
        return False

    allowed = (await session.execute(
        select(AdminPermission.allowed).where(
            AdminPermission.role_id == role.id,
            AdminPermission.permission.in_([permission, OWNER_PERMISSION]),
        )
    )).scalars().all()
    return any(allowed)
