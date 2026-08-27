from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminPermission, AdminRole, GroupOwner, NetworkAdmin
from groupbot.network_models import Network, NetworkGroup


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


async def _network_permission(session: AsyncSession, chat_id: int, user_id: int, permission: str) -> bool:
    owner_id = (await session.execute(
        select(GroupOwner.user_id).where(
            GroupOwner.chat_id == chat_id,
            GroupOwner.is_current.is_(True),
        ).limit(1)
    )).scalar_one_or_none()
    if owner_id is None:
        return False

    network_group_id = (await session.execute(
        select(NetworkGroup.id)
        .join(Network, Network.id == NetworkGroup.network_id)
        .where(
            NetworkGroup.chat_id == chat_id,
            Network.owner_user_id == owner_id,
        )
        .limit(1)
    )).scalar_one_or_none()
    if network_group_id is None:
        return False

    row = (await session.execute(
        select(NetworkAdmin.permissions_json).where(
            NetworkAdmin.owner_user_id == owner_id,
            NetworkAdmin.user_id == user_id,
            NetworkAdmin.is_active.is_(True),
        )
    )).scalar_one_or_none()
    if row is None:
        return False
    permissions = {str(value) for value in (row or [])}
    return permission in permissions or OWNER_PERMISSION in permissions


async def has_permission(session: AsyncSession, chat_id: int, user_id: int, permission: str) -> bool:
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
    if role is not None:
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
        if any(allowed):
            return True

    return await _network_permission(session, chat_id, user_id, permission)
