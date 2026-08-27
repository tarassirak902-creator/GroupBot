from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import User
from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import _format_expiry, _group_ready, _load_users, _warning_limit
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.permissions import has_permission


GLOBAL_LIST_COMMANDS = {
    "банлист": "ban",
    "мутлист": "mute",
    "преды": "warning",
}


def create_admin_punishment_lists_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admin_punishment_lists")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.casefold().in_(set(GLOBAL_LIST_COMMANDS)),
    )
    async def global_punishment_list(message: Message) -> None:
        if message.from_user is None:
            return
        normalized = " ".join((message.text or "").strip().split()).casefold()
        action = GLOBAL_LIST_COMMANDS.get(normalized)
        if action is None:
            return

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            if not await has_permission(
                session,
                message.chat.id,
                message.from_user.id,
                "punishment_lists",
            ):
                await message.reply("Недостаточно прав Mimorus для просмотра общих списков наказаний.")
                return

            warning_limit = await _warning_limit(session, message.chat.id)
            now = datetime.now(timezone.utc)
            conditions = [
                ModerationAction.chat_id == message.chat.id,
                ModerationAction.action == action,
                ModerationAction.is_active.is_(True),
            ]
            if action == "mute":
                conditions.append(
                    or_(
                        ModerationAction.expires_at.is_(None),
                        ModerationAction.expires_at > now,
                    )
                )
            rows = list((
                await session.execute(
                    select(ModerationAction)
                    .where(*conditions)
                    .order_by(ModerationAction.created_at.desc())
                    .limit(30)
                )
            ).scalars().all())
            users = await _load_users(
                session,
                {row.target_user_id for row in rows} | {row.actor_user_id for row in rows},
            )

        title = {
            "банлист": "📋 Банлист",
            "мутлист": "📋 Мутлист",
            "преды": "📋 Предупреждения",
        }[normalized]
        lines = [f"<b>{title}</b>", ""]
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
                index = min(int(row.warning_index or 0), warning_limit) if row.warning_index else "?"
                lines.append(
                    f"• {target_text} — {index}/{warning_limit}; выдал: {actor_text}; причина: {reason}"
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
