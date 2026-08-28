from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import User
from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import (
    _admin_access,
    _format_expiry,
    _group_ready,
    _load_users,
    _warning_limit,
)
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.permissions import has_permission


LIST_COMMANDS: dict[str, tuple[str, bool]] = {
    "банлист": ("ban", False),
    "мутлист": ("mute", False),
    "преды": ("warning", False),
    "мои баны": ("ban", True),
    "мои муты": ("mute", True),
    "выдал пред": ("warning", True),
}


def _latest_by_target(rows: list[ModerationAction], limit: int = 30) -> list[ModerationAction]:
    result: list[ModerationAction] = []
    seen: set[int] = set()
    for row in rows:
        if row.target_user_id in seen:
            continue
        seen.add(row.target_user_id)
        result.append(row)
        if len(result) >= limit:
            break
    return result


async def _active_warning_totals(
    session: AsyncSession,
    *,
    chat_id: int,
    user_ids: set[int],
) -> dict[int, int]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(ModerationAction.target_user_id, func.count())
            .where(
                ModerationAction.chat_id == chat_id,
                ModerationAction.target_user_id.in_(user_ids),
                ModerationAction.action == "warning",
                ModerationAction.is_active.is_(True),
            )
            .group_by(ModerationAction.target_user_id)
        )
    ).all()
    return {int(user_id): int(count) for user_id, count in rows}


async def _warning_state_rows(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_user_id: int | None,
) -> tuple[list[ModerationAction], dict[int, int], dict[int, int]]:
    conditions = [
        ModerationAction.chat_id == chat_id,
        ModerationAction.action == "warning",
        ModerationAction.is_active.is_(True),
    ]
    if actor_user_id is not None:
        conditions.append(ModerationAction.actor_user_id == actor_user_id)

    rows = list(
        (
            await session.execute(
                select(ModerationAction)
                .where(*conditions)
                .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
                .limit(300)
            )
        ).scalars().all()
    )
    latest = _latest_by_target(rows)
    target_ids = {row.target_user_id for row in latest}
    total_counts = await _active_warning_totals(session, chat_id=chat_id, user_ids=target_ids)

    own_counts: dict[int, int] = defaultdict(int)
    if actor_user_id is not None:
        for row in rows:
            if row.target_user_id in target_ids:
                own_counts[row.target_user_id] += 1
    return latest, total_counts, dict(own_counts)


async def _punishment_state_rows(
    session: AsyncSession,
    *,
    chat_id: int,
    action: str,
    actor_user_id: int | None,
) -> list[ModerationAction]:
    now = datetime.now(timezone.utc)
    conditions = [
        ModerationAction.chat_id == chat_id,
        ModerationAction.action == action,
        ModerationAction.is_active.is_(True),
    ]
    if action == "mute":
        conditions.append(
            or_(ModerationAction.expires_at.is_(None), ModerationAction.expires_at > now)
        )
    if actor_user_id is not None:
        conditions.append(ModerationAction.actor_user_id == actor_user_id)

    rows = list(
        (
            await session.execute(
                select(ModerationAction)
                .where(*conditions)
                .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
                .limit(300)
            )
        ).scalars().all()
    )
    return _latest_by_target(rows)


def _title(command: str) -> str:
    return {
        "банлист": "📋 Банлист",
        "мутлист": "📋 Мутлист",
        "преды": "📋 Предупреждения",
        "мои баны": "📋 Мои баны",
        "мои муты": "📋 Мои муты",
        "выдал пред": "📋 Выданные предупреждения",
    }[command]


def create_admin_punishment_lists_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admin_punishment_lists")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.casefold().in_(set(LIST_COMMANDS)),
    )
    async def punishment_list(message: Message) -> None:
        if message.from_user is None:
            return
        normalized = " ".join((message.text or "").strip().split()).casefold()
        spec = LIST_COMMANDS.get(normalized)
        if spec is None:
            return
        action, personal = spec

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            if personal:
                if not await _admin_access(session, message.chat.id, message.from_user.id):
                    await message.reply("Недостаточно прав Mimorus для этой команды.")
                    return
            elif not await has_permission(
                session,
                message.chat.id,
                message.from_user.id,
                "punishment_lists",
            ):
                await message.reply(
                    "Недостаточно прав Mimorus для просмотра общих списков наказаний."
                )
                return

            actor_filter = message.from_user.id if personal else None
            warning_limit = await _warning_limit(session, message.chat.id)

            warning_totals: dict[int, int] = {}
            own_warning_counts: dict[int, int] = {}
            if action == "warning":
                rows, warning_totals, own_warning_counts = await _warning_state_rows(
                    session,
                    chat_id=message.chat.id,
                    actor_user_id=actor_filter,
                )
            else:
                rows = await _punishment_state_rows(
                    session,
                    chat_id=message.chat.id,
                    action=action,
                    actor_user_id=actor_filter,
                )

            users = await _load_users(
                session,
                {row.target_user_id for row in rows} | {row.actor_user_id for row in rows},
            )

        lines = [f"<b>{_title(normalized)}</b>", ""]
        if not rows:
            lines.append("Список пуст.")

        for row in rows:
            target_text = (
                clickable_user_display(users[row.target_user_id])
                if row.target_user_id in users
                else "Пользователь"
            )
            actor_text = (
                clickable_user_display(users[row.actor_user_id])
                if row.actor_user_id in users
                else "Администратор"
            )
            reason = escape(row.reason or "не указана")

            if row.action == "warning":
                total = min(warning_totals.get(row.target_user_id, 0), warning_limit)
                if personal:
                    own = own_warning_counts.get(row.target_user_id, 0)
                    lines.append(
                        f"• {target_text} — ваших активных: <b>{own}</b>; "
                        f"всего: <b>{total}/{warning_limit}</b>; "
                        f"последняя причина: {reason}"
                    )
                else:
                    lines.append(
                        f"• {target_text} — <b>{total}/{warning_limit}</b>; "
                        f"последнее выдал: {actor_text}; причина: {reason}"
                    )
            elif row.action == "mute":
                lines.append(
                    f"• {target_text} — до {_format_expiry(row.expires_at)}; "
                    f"выдал: {actor_text}; причина: {reason}"
                )
            else:
                lines.append(
                    f"• {target_text} — выдал: {actor_text}; причина: {reason}"
                )

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    return router
