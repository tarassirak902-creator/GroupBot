from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole, GroupSettings
from groupbot.services.permissions import is_group_owner

DEPUTY = "Зам. владельца"
CHIEF = "Глав. админ"


async def _actor_role_name(session: AsyncSession, *, chat_id: int, actor_id: int) -> str | None:
    return (
        await session.execute(
            select(AdminRole.name)
            .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == actor_id,
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
    """Return a user-facing error when manual punishment of target is forbidden.

    Approved project rules:
    - Group owner can manually punish VIP and Nedotroga.
    - VIP can additionally be punished only by Deputy Owner.
    - Nedotroga can additionally be punished by Deputy Owner or Chief Admin.
    Other rank-vs-rank punishment rules are intentionally not invented here.
    """
    if await is_group_owner(session, chat_id, target_id):
        return "Владельца группы нельзя наказать."

    if await is_group_owner(session, chat_id, actor_id):
        return None

    special = await _special_statuses(session, chat_id)
    vip_ids = _ids(special.get("vip"))
    nedotroga_ids = _ids(special.get("nedotroga"))

    if target_id not in vip_ids and target_id not in nedotroga_ids:
        return None

    actor_role = await _actor_role_name(session, chat_id=chat_id, actor_id=actor_id)

    if target_id in vip_ids:
        if actor_role == DEPUTY:
            return None
        return "💎 VIP-пользователя может наказать только Владелец группы или Зам. владельца."

    if target_id in nedotroga_ids:
        if actor_role in {DEPUTY, CHIEF}:
            return None
        return "🛡 Недотрогу может наказать только Владелец группы, Зам. владельца или Глав. админ."

    return None
