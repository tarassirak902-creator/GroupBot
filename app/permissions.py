from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAssignment, AdminPermission, AdminRole, GroupOwner


async def is_registered_owner(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    row = await session.scalar(
        select(GroupOwner.id).where(
            GroupOwner.chat_id == chat_id,
            GroupOwner.user_id == user_id,
            GroupOwner.is_active.is_(True),
        ).limit(1)
    )
    return row is not None


async def is_telegram_owner(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status == "creator"


async def is_telegram_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in {"creator", "administrator"}


async def is_group_owner(session: AsyncSession, bot: Bot, chat_id: int, user_id: int) -> bool:
    if await is_registered_owner(session, chat_id, user_id):
        return True
    return await is_telegram_owner(bot, chat_id, user_id)


async def has_permission(
    session: AsyncSession,
    bot: Bot,
    chat_id: int,
    user_id: int,
    permission: str,
) -> bool:
    """Resolve owner, custom role permissions, then Telegram admin fallback.

    Until a group has explicit custom roles, existing Telegram administrators keep
    their current access so Phase 0 does not break the running bot. Later phases
    can move individual commands to explicit permission keys incrementally.
    """
    if await is_group_owner(session, bot, chat_id, user_id):
        return True

    explicit = await session.scalar(
        select(AdminPermission.is_allowed)
        .join(AdminRole, AdminRole.id == AdminPermission.role_id)
        .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
        .where(
            AdminAssignment.chat_id == chat_id,
            AdminAssignment.user_id == user_id,
            AdminRole.chat_id == chat_id,
            AdminRole.is_active.is_(True),
            AdminPermission.permission == permission,
        )
        .order_by(AdminPermission.is_allowed.asc())
        .limit(1)
    )
    if explicit is not None:
        return bool(explicit)

    return await is_telegram_admin(bot, chat_id, user_id)
