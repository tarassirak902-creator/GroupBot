from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole, GroupSettings
from groupbot.services.permissions import is_group_owner

DEPUTY = "Зам. владельца"
CHIEF = "Глав. админ"
CHAT_ADMIN = "Администратор чата"
VOICE_ADMIN = "Администратор войса"
HELPER = "Помощник"

# Lower number means higher position. Chat and voice administrators are equal.
STANDARD_RANK_LEVEL = {
    DEPUTY: 1,
    CHIEF: 2,
    CHAT_ADMIN: 3,
    VOICE_ADMIN: 3,
}


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


async def _special_statuses(session: AsyncSession, chat_id: int) -> dict:
    config = (
        await session.execute(
            select(GroupSettings.moderation_config).where(GroupSettings.chat_id == chat_id)
        )
    ).scalar_one_or_none() or {}
    return dict(config.get("special_statuses") or {})


def _ids(values) -> set[int]:
    result: set[int] = set()
    for value in values or []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


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
        return "Владельца группы нельзя наказать."

    if await is_group_owner(session, chat_id, actor_id):
        return None

    actor_role = await _role_name(session, chat_id=chat_id, user_id=actor_id)
    if actor_role == HELPER:
        return "Помощник не может выдавать наказания. Используйте ответом на сообщение команду «нарушение»."

    special = await _special_statuses(session, chat_id)
    vip_ids = _ids(special.get("vip"))
    nedotroga_ids = _ids(special.get("nedotroga"))

    if target_id in vip_ids:
        if actor_role == DEPUTY:
            return None
        return "💎 VIP-пользователя может наказать только Владелец группы или Зам. владельца."

    if target_id in nedotroga_ids:
        if actor_role in {DEPUTY, CHIEF}:
            return None
        return "🛡 Недотрогу может наказать только Владелец группы, Зам. владельца или Глав. админ."

    target_role = await _role_name(session, chat_id=chat_id, user_id=target_id)
    if target_role is None or target_role == HELPER:
        return None

    actor_level = STANDARD_RANK_LEVEL.get(actor_role or "")
    target_level = STANDARD_RANK_LEVEL.get(target_role)

    if target_level is None:
        return "Пользователя с собственным административным рангом может наказать только Владелец группы, пока для таких рангов не задана иерархия."

    if actor_level is None:
        return "Ваш административный ранг не имеет места в стандартной иерархии и не может наказывать других администраторов."

    if actor_level < target_level:
        return None

    if actor_level == target_level:
        return "Нельзя наказать администратора равного вам уровня."

    return "Нельзя наказать администратора, который находится выше вас по иерархии."
