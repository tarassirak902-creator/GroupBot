from __future__ import annotations

import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import User
from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import _group_ready, _unmuted_permissions
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.permissions import has_permission
from groupbot.services.users import upsert_user

COMMAND_RE = re.compile(
    r"^(разбан|размут|снять\s+пред(?:ы)?)(?:\s+(.+))?$",
    re.IGNORECASE,
)
TG_ID_RE = re.compile(r"^tg://user\?id=(\d+)$", re.IGNORECASE)


async def _resolve_target(
    session: AsyncSession,
    *,
    message: Message,
    token: str | None,
) -> User | None:
    if token:
        raw = token.strip()
        tg_match = TG_ID_RE.match(raw)
        if tg_match:
            raw = tg_match.group(1)
        if raw.startswith("@"):
            raw = raw[1:]

        if raw.isdigit():
            return (
                await session.execute(
                    select(User).where(User.telegram_user_id == int(raw))
                )
            ).scalar_one_or_none()

        if raw:
            return (
                await session.execute(
                    select(User).where(func.lower(User.username) == raw.casefold())
                )
            ).scalar_one_or_none()

    reply = message.reply_to_message
    if reply is None or reply.from_user is None or reply.from_user.is_bot:
        return None
    await upsert_user(session, reply.from_user)
    await session.flush()
    return (
        await session.execute(
            select(User).where(User.telegram_user_id == reply.from_user.id)
        )
    ).scalar_one_or_none()


async def _deactivate_actions(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    action: str,
) -> int:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(ModerationAction)
        .where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_user_id == user_id,
            ModerationAction.action == action,
            ModerationAction.is_active.is_(True),
        )
        .values(is_active=False, revoked_at=now)
    )
    return int(result.rowcount or 0)


def create_moderation_release_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="moderation_release")

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def release_command(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        match = COMMAND_RE.match(" ".join((message.text or "").strip().split()))
        if not match:
            return

        command = match.group(1).casefold()
        token = match.group(2)
        if command == "разбан":
            permission = "unban"
        elif command == "размут":
            permission = "unmute"
        else:
            permission = "warning"

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            if not await has_permission(session, message.chat.id, message.from_user.id, permission):
                await message.reply("Недостаточно прав Mimorus для этой команды.")
                return
            async with session.begin_nested():
                target = await _resolve_target(session, message=message, token=token)
            if target is None:
                await message.reply(
                    "Не удалось найти пользователя. Используйте reply, @username, Telegram ID или tg://user?id=..."
                )
                return
            if target.telegram_user_id == message.from_user.id:
                await message.reply("Нельзя применить эту команду к себе.")
                return

        identity = clickable_user_display(target)
        target_id = target.telegram_user_id

        try:
            if command == "разбан":
                await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
                async with session_factory() as session:
                    async with session.begin():
                        await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="ban")
                        # Reset warnings explicitly as well as through the DB trigger.
                        await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="warning")
                        await write_audit(
                            session,
                            "moderation.unban",
                            chat_id=message.chat.id,
                            actor_user_id=message.from_user.id,
                            target_type="user",
                            target_id=str(target_id),
                            payload={"warnings_reset": True},
                        )
                await message.answer(
                    f"✅ {identity} разбанен.\n⚠️ Предупреждения: <b>0/5</b>.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            if command == "размут":
                await bot.restrict_chat_member(
                    message.chat.id,
                    target_id,
                    permissions=_unmuted_permissions(),
                )
                async with session_factory() as session:
                    async with session.begin():
                        await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="mute")
                        await write_audit(
                            session,
                            "moderation.unmute",
                            chat_id=message.chat.id,
                            actor_user_id=message.from_user.id,
                            target_type="user",
                            target_id=str(target_id),
                            payload={},
                        )
                await message.answer(
                    f"🔊 Мут снят с {identity}.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            async with session_factory() as session:
                async with session.begin():
                    removed = await _deactivate_actions(
                        session,
                        chat_id=message.chat.id,
                        user_id=target_id,
                        action="warning",
                    )
                    await write_audit(
                        session,
                        "moderation.warnings_clear",
                        chat_id=message.chat.id,
                        actor_user_id=message.from_user.id,
                        target_type="user",
                        target_id=str(target_id),
                        payload={"removed": removed},
                    )
            await message.answer(
                f"✅ Предупреждения {identity} сняты.\n⚠️ Предупреждения: <b>0/5</b>.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            await message.reply(f"Не удалось выполнить действие через Telegram: {str(exc)[:300]}")

    return router
