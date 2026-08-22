from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminPermission, GroupOwner


OWNER_PERMISSION = "*"


async def is_group_owner(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    return (await session.execute(
        select(GroupOwner.id).where(
            GroupOwner.chat_id == chat_id,
            GroupOwner.user_id == user_id,
            GroupOwner.is_current.is_(True),
        )
    )).scalar_one_or_none() is not None


async def has_permission(session: AsyncSession, chat_id: int, user_id: int, permission: str) -> bool:
    if await is_group_owner(session, chat_id, user_id):
        return True

    assignment = (await session.execute(
        select(AdminAssignment).where(
            AdminAssignment.chat_id == chat_id,
            AdminAssignment.user_id == user_id,
        )
    )).scalar_one_or_none()
    if assignment is None or assignment.role_id is None:
        return False

    allowed = (await session.execute(
        select(AdminPermission.allowed).where(
            AdminPermission.role_id == assignment.role_id,
            AdminPermission.permission.in_([permission, OWNER_PERMISSION]),
        )
    )).scalars().all()
    return any(allowed)
