from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import GroupMember, GroupSettings, MemberStatus


SPECIAL_STATUS_KEYS = ("vip", "nedotroga")


def status_ids(values) -> set[int]:
    result: set[int] = set()
    for value in values or []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def special_status_ids(config: dict | None, status: str) -> set[int]:
    special = dict((config or {}).get("special_statuses") or {})
    return status_ids(special.get(status))


def special_statuses_for_user(config: dict | None, user_id: int) -> list[str]:
    result: list[str] = []
    if user_id in special_status_ids(config, "vip"):
        result.append("💎 VIP")
    if user_id in special_status_ids(config, "nedotroga"):
        result.append("🛡 Недотрога")
    return result


def remove_special_statuses_from_config(config: dict | None, user_id: int) -> tuple[dict, list[str]]:
    root = dict(config or {})
    special = dict(root.get("special_statuses") or {})
    removed: list[str] = []
    changed = False
    for status in SPECIAL_STATUS_KEYS:
        raw_values = list(special.get(status) or [])
        filtered: list[int] = []
        found = False
        for value in raw_values:
            try:
                normalized = int(value)
            except (TypeError, ValueError):
                continue
            if normalized == user_id:
                found = True
                continue
            if normalized not in filtered:
                filtered.append(normalized)
        if found:
            removed.append(status)
            changed = True
        if filtered != raw_values:
            changed = True
        special[status] = filtered
    if changed:
        root["special_statuses"] = special
    return root, removed


async def remove_special_statuses_for_user(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> list[str]:
    settings = (
        await session.execute(
            select(GroupSettings)
            .where(GroupSettings.chat_id == chat_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if settings is None:
        return []
    updated, removed = remove_special_statuses_from_config(settings.moderation_config, user_id)
    if removed:
        settings.moderation_config = updated
    return removed


async def is_active_group_member(session: AsyncSession, *, chat_id: int, user_id: int) -> bool:
    status = (
        await session.execute(
            select(GroupMember.status).where(
                GroupMember.chat_id == chat_id,
                GroupMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return status == MemberStatus.member.value
