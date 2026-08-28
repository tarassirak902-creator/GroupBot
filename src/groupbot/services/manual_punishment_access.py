from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole, GroupSettings
from groupbot.services.permissions import is_group_owner
from groupbot.services.special_statuses import is_active_group_member, special_status_ids

DEPUTY = "Зам. владельца"
CHIEF = "Глав. админ"
CHAT_ADMIN = "Администратор чата"
VOICE_ADMIN = "Администратор войса"
HELPER = "Помощник"


async def _role_name(session: AsyncSession, *, chat_id: int, user_id: int) -> str | None:
    return (
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


async def _moderation_config(session: AsyncSession, chat_id: int) -> dict:
    return (
        await session.execute(
            select(GroupSettings.moderation_config).where(GroupSettings.chat_id == chat_id)
        )
    ).scalar_one_or_none() or {}


async def manual_punishment_error(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
    target_id: int,
) -> str | None:
    """Return a user-facing error when manual punishment of target is forbidden."""
    if actor_id == target_id:
        return "Нельзя применить наказание к себе."

    if await is_group_owner(session, chat_id, target_id):
        return "⛔ Владельца группы нельзя наказать командами модерации Mimorus."

    actor_role = await _role_name(session, chat_id=chat_id, user_id=actor_id)
    if actor_role == HELPER:
        return "Помощник не может выдавать наказания. Используйте ответом на сообщение команду «нарушение»."

    # Owner is the top-level override for every non-owner target.
    if await is_group_owner(session, chat_id, actor_id):
        return None

    target_role = await _role_name(session, chat_id=chat_id, user_id=target_id)
    if target_role is not None and target_role != HELPER:
        return "⛔ Нельзя наказать администратора Mimorus. Это может сделать только Владелец группы."

    # Special statuses only have meaning while the target is an active group
    # participant. Exit cleanup normally removes them; this membership guard also
    # prevents stale historical JSON from granting immunity after a later rejoin.
    if not await is_active_group_member(session, chat_id=chat_id, user_id=target_id):
        return None
    config = await _moderation_config(session, chat_id)

    if target_id in special_status_ids(config, "vip"):
        if actor_role == DEPUTY:
            return None
        return "💎 VIP-пользователя может наказать только Владелец группы или Зам. владельца."

    if target_id in special_status_ids(config, "nedotroga"):
        if actor_role in {DEPUTY, CHIEF}:
            return None
        return "🛡 Недотрогу может наказать только Владелец группы, Зам. владельца или Глав. админ."

    return None
