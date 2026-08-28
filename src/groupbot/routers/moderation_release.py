from __future__ import annotations

import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import User
from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import _group_ready
from groupbot.routers.user_display import clickable_identity
from groupbot.services.audit import write_audit
from groupbot.services.moderation_state import restore_member_permissions
from groupbot.services.permissions import has_permission, is_group_owner
from groupbot.services.users import upsert_user

COMMAND_RE = re.compile(
    r"^(разбан|размут|снять\s+преды|снять\s+пред)(?:\s+(.+))?$",
    re.IGNORECASE,
)
RELEASE_FILTER_RE = re.compile(
    r"^\s*(?:разбан|размут|снять\s+преды|снять\s+пред)(?:\s+.+)?\s*$",
    re.IGNORECASE,
)
TG_ID_RE = re.compile(r"^tg://user\?id=(\d+)$", re.IGNORECASE)


async def _resolve_target(session: AsyncSession, *, message: Message, token: str | None) -> User | None:
    if token:
        raw = token.strip()
        tg_match = TG_ID_RE.match(raw)
        if tg_match:
            raw = tg_match.group(1)
        if raw.startswith("@"):
            raw = raw[1:]
        if raw.isdigit():
            return (await session.execute(select(User).where(User.telegram_user_id == int(raw)))).scalar_one_or_none()
        if raw:
            return (await session.execute(select(User).where(func.lower(User.username) == raw.casefold()))).scalar_one_or_none()
    reply = message.reply_to_message
    if reply is None or reply.from_user is None or reply.from_user.is_bot:
        return None
    await upsert_user(session, reply.from_user)
    await session.flush()
    return (await session.execute(select(User).where(User.telegram_user_id == reply.from_user.id))).scalar_one_or_none()


async def _deactivate_actions(session: AsyncSession, *, chat_id: int, user_id: int, action: str) -> int:
    result = await session.execute(
        update(ModerationAction)
        .where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_user_id == user_id,
            ModerationAction.action == action,
            ModerationAction.is_active.is_(True),
        )
        .values(is_active=False, revoked_at=datetime.now(timezone.utc))
    )
    return int(result.rowcount or 0)


async def _deactivate_one_warning(session: AsyncSession, *, chat_id: int, user_id: int) -> int:
    warning_id = (
        await session.execute(
            select(ModerationAction.id)
            .where(
                ModerationAction.chat_id == chat_id,
                ModerationAction.target_user_id == user_id,
                ModerationAction.action == "warning",
                ModerationAction.is_active.is_(True),
            )
            .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if warning_id is None:
        return 0
    result = await session.execute(
        update(ModerationAction)
        .where(ModerationAction.id == warning_id)
        .values(is_active=False, revoked_at=datetime.now(timezone.utc))
    )
    return int(result.rowcount or 0)


async def _active_warning_count(session: AsyncSession, *, chat_id: int, user_id: int) -> int:
    return int((await session.execute(
        select(func.count()).select_from(ModerationAction).where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_user_id == user_id,
            ModerationAction.action == "warning",
            ModerationAction.is_active.is_(True),
        )
    )).scalar_one())


def _notification_identity_from_tg(user) -> str:
    return clickable_identity(
        telegram_user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=None,
    )


def _notification_identity_from_db(user: User) -> str:
    return clickable_identity(
        telegram_user_id=user.telegram_user_id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=None,
    )


async def _actor_title(session_factory: async_sessionmaker[AsyncSession], chat_id: int, user) -> str:
    async with session_factory() as session:
        owner = await is_group_owner(session, chat_id, user.id)
    prefix = "Владелец группы" if owner else "Администратор"
    return f"{prefix} {_notification_identity_from_tg(user)}"


def create_moderation_release_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="moderation_release")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(RELEASE_FILTER_RE),
    )
    async def release_command(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        match = COMMAND_RE.match(" ".join((message.text or "").strip().split()))
        if not match:
            return
        command = match.group(1).casefold()
        token = match.group(2)
        permission = "unban" if command == "разбан" else "unmute" if command == "размут" else "warning"

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            if not await has_permission(session, message.chat.id, message.from_user.id, permission):
                await message.reply("Недостаточно прав Mimorus для этой команды.")
                return
            async with session.begin_nested():
                target = await _resolve_target(session, message=message, token=token)
            if target is None:
                await message.reply("Не удалось найти пользователя. Используйте reply, @username, Telegram ID или tg://user?id=...")
                return
            if target.telegram_user_id == message.from_user.id:
                await message.reply("Нельзя применить эту команду к себе.")
                return

        identity = _notification_identity_from_db(target)
        actor = await _actor_title(session_factory, message.chat.id, message.from_user)
        target_id = target.telegram_user_id

        try:
            if command == "разбан":
                await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
                async with session_factory() as session:
                    async with session.begin():
                        await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="ban")
                        await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="warning")
                        await write_audit(session, "moderation.unban", chat_id=message.chat.id, actor_user_id=message.from_user.id, target_type="user", target_id=str(target_id), payload={"warnings_reset": True})
                await message.answer(
                    f"✅ {actor} разбанил {identity}.\n\nАктивных предупреждений: 0.",
                    parse_mode="HTML", disable_web_page_preview=True,
                )
                return

            if command == "размут":
                await restore_member_permissions(bot, message.chat.id, target_id)
                async with session_factory() as session:
                    async with session.begin():
                        await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="mute")
                        remaining = await _active_warning_count(session, chat_id=message.chat.id, user_id=target_id)
                        await write_audit(session, "moderation.unmute", chat_id=message.chat.id, actor_user_id=message.from_user.id, target_type="user", target_id=str(target_id), payload={})
                await message.answer(
                    f"✅ {actor} снял мут с {identity}.\n\nАктивных предупреждений: {remaining}.",
                    parse_mode="HTML", disable_web_page_preview=True,
                )
                return

            if command == "снять пред":
                async with session_factory() as session:
                    async with session.begin():
                        removed = await _deactivate_one_warning(session, chat_id=message.chat.id, user_id=target_id)
                        remaining = await _active_warning_count(session, chat_id=message.chat.id, user_id=target_id)
                        await write_audit(session, "moderation.warning_removed", chat_id=message.chat.id, actor_user_id=message.from_user.id, target_type="user", target_id=str(target_id), payload={"removed": removed, "remaining": remaining})
                if removed == 0:
                    text = f"⚠️ У {identity} нет активных предупреждений.\n\nАктивных предупреждений: {remaining}."
                else:
                    text = f"✅ {actor} снял последнее предупреждение с {identity}.\n\nАктивных предупреждений: {remaining}."
                await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
                return

            async with session_factory() as session:
                async with session.begin():
                    removed = await _deactivate_actions(session, chat_id=message.chat.id, user_id=target_id, action="warning")
                    await write_audit(session, "moderation.warnings_clear", chat_id=message.chat.id, actor_user_id=message.from_user.id, target_type="user", target_id=str(target_id), payload={"removed": removed})
            await message.answer(
                f"✅ {actor} снял все предупреждения с {identity}.\n\nАктивных предупреждений: 0.",
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as exc:
            await message.reply(f"Не удалось выполнить действие через Telegram: {str(exc)[:300]}")

    return router
