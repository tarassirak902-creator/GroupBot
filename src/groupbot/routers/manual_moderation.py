from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import ChatPermissions, Message
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, Group, GroupSettings, GroupStatus, User
from groupbot.moderation_models import ModerationAction
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.permissions import has_permission, is_group_owner
from groupbot.services.subscriptions import active_subscription_for_group
from groupbot.services.users import upsert_user

ACTION_ALIASES = {
    "пред": "warning",
    "мут": "mute",
    "бан": "ban",
    "размут": "unmute",
    "разбан": "unban",
}
LIST_COMMANDS = {
    "мои баны": ("ban", True),
    "мои муты": ("mute", True),
    "выдал пред": ("warning", True),
    "банлист": ("ban", False),
    "мутлист": ("mute", False),
    "преды": ("warning", False),
}
DURATION_RE = re.compile(r"^(\d+)(м|мин|ч|д)$", re.IGNORECASE)


def _identity_from_tg(user) -> str:
    return clickable_identity(
        telegram_user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
    )


def _parse_command(text: str) -> tuple[str | None, list[str]]:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return None, []
    parts = normalized.split(" ")
    action = ACTION_ALIASES.get(parts[0].casefold())
    return action, parts[1:]


def _duration(token: str) -> timedelta | None:
    match = DURATION_RE.match(token)
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    unit = match.group(2).casefold()
    if unit in {"м", "мин"}:
        return timedelta(minutes=value)
    if unit == "ч":
        return timedelta(hours=value)
    if unit == "д":
        return timedelta(days=value)
    return None


async def _group_ready(session: AsyncSession, chat_id: int) -> bool:
    status = (
        await session.execute(select(Group.status).where(Group.chat_id == chat_id))
    ).scalar_one_or_none()
    if status != GroupStatus.active.value:
        return False
    return await active_subscription_for_group(session, chat_id) is not None


async def _admin_access(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    if await is_group_owner(session, chat_id, user_id):
        return True
    assignment = (
        await session.execute(
            select(AdminAssignment.id).where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return assignment is not None


async def _command_mode(session: AsyncSession, chat_id: int) -> str:
    config = (
        await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id == chat_id))
    ).scalar_one_or_none() or {}
    return str(config.get("admin_command_mode", "both"))


async def _record_action(
    session: AsyncSession,
    *,
    chat_id: int,
    target_user_id: int,
    actor_user_id: int,
    action: str,
    reason: str | None = None,
    warning_index: int | None = None,
    expires_at: datetime | None = None,
    source: str = "manual",
) -> ModerationAction:
    row = ModerationAction(
        chat_id=chat_id,
        target_user_id=target_user_id,
        actor_user_id=actor_user_id,
        action=action,
        reason=reason,
        warning_index=warning_index,
        expires_at=expires_at,
        is_active=True,
        source=source,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        f"moderation.{action}",
        chat_id=chat_id,
        actor_user_id=actor_user_id,
        target_type="user",
        target_id=str(target_user_id),
        payload={
            "reason": reason,
            "warning_index": warning_index,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "source": source,
        },
    )
    return row


async def _deactivate(
    session: AsyncSession,
    *,
    chat_id: int,
    target_user_id: int,
    action: str,
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(ModerationAction)
        .where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_user_id == target_user_id,
            ModerationAction.action == action,
            ModerationAction.is_active.is_(True),
        )
        .values(is_active=False, revoked_at=now)
    )


async def _warning_count(session: AsyncSession, chat_id: int, target_user_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(ModerationAction).where(
                ModerationAction.chat_id == chat_id,
                ModerationAction.target_user_id == target_user_id,
                ModerationAction.action == "warning",
                ModerationAction.is_active.is_(True),
            )
        )
    ).scalar_one()


def _unmuted_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )


async def _apply_warning_scale(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_user_id: int,
    actor_user_id: int,
    count: int,
    reason: str | None,
) -> str | None:
    now = datetime.now(timezone.utc)
    if count == 3:
        until = now + timedelta(minutes=15)
        await bot.restrict_chat_member(chat_id, target_user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        await _record_action(
            session,
            chat_id=chat_id,
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            action="mute",
            reason="Автодействие шкалы предупреждений 3/5",
            expires_at=until,
            source="warning_scale",
        )
        return "🔇 Автодействие: мут на 15 минут."
    if count == 4:
        until = now + timedelta(hours=1)
        await bot.restrict_chat_member(chat_id, target_user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        await _record_action(
            session,
            chat_id=chat_id,
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            action="mute",
            reason="Автодействие шкалы предупреждений 4/5",
            expires_at=until,
            source="warning_scale",
        )
        return "🔇 Автодействие: мут на 1 час."
    if count >= 5:
        await bot.ban_chat_member(chat_id, target_user_id)
        await _record_action(
            session,
            chat_id=chat_id,
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            action="ban",
            reason="Автодействие шкалы предупреждений 5/5",
            source="warning_scale",
        )
        return "⛔ Автодействие: бан."
    return None


async def _load_users(session: AsyncSession, ids: set[int]) -> dict[int, User]:
    if not ids:
        return {}
    rows = (await session.execute(select(User).where(User.telegram_user_id.in_(ids)))).scalars().all()
    return {row.telegram_user_id: row for row in rows}


def _format_expiry(value: datetime | None) -> str:
    if value is None:
        return "без срока"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def create_manual_moderation_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="manual_moderation")

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def moderation_text(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        raw = (message.text or "").strip()
        normalized = " ".join(raw.split()).casefold()

        if normalized in LIST_COMMANDS:
            action, personal = LIST_COMMANDS[normalized]
            async with session_factory() as session:
                if not await _group_ready(session, message.chat.id) or not await _admin_access(session, message.chat.id, message.from_user.id):
                    return
                now = datetime.now(timezone.utc)
                conditions = [
                    ModerationAction.chat_id == message.chat.id,
                    ModerationAction.action == action,
                    ModerationAction.is_active.is_(True),
                ]
                if action == "mute":
                    conditions.append(or_(ModerationAction.expires_at.is_(None), ModerationAction.expires_at > now))
                if personal:
                    conditions.append(ModerationAction.actor_user_id == message.from_user.id)
                rows = list((await session.execute(
                    select(ModerationAction).where(*conditions).order_by(ModerationAction.created_at.desc()).limit(30)
                )).scalars().all())
                user_ids = {r.target_user_id for r in rows} | {r.actor_user_id for r in rows}
                users = await _load_users(session, user_ids)

            title = {
                "мои баны": "📋 Мои баны",
                "мои муты": "📋 Мои муты",
                "выдал пред": "📋 Выданные предупреждения",
                "банлист": "📋 Банлист",
                "мутлист": "📋 Мутлист",
                "преды": "📋 Предупреждения",
            }[normalized]
            lines = [f"<b>{title}</b>", ""]
            if not rows:
                lines.append("Список пуст.")
            else:
                for row in rows:
                    target = users.get(row.target_user_id)
                    actor = users.get(row.actor_user_id)
                    target_text = clickable_user_display(target) if target else "Пользователь"
                    actor_text = clickable_user_display(actor) if actor else "Администратор"
                    reason = escape(row.reason or "не указана")
                    if row.action == "warning":
                        lines.append(f"• {target_text} — {row.warning_index or '?'} / 5; выдал: {actor_text}; причина: {reason}")
                    elif row.action == "mute":
                        lines.append(f"• {target_text} — до {_format_expiry(row.expires_at)}; выдал: {actor_text}; причина: {reason}")
                    else:
                        lines.append(f"• {target_text} — выдал: {actor_text}; причина: {reason}")
            await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
            return

        action, args = _parse_command(raw)
        if action is None:
            return
        if message.reply_to_message is None or message.reply_to_message.from_user is None:
            await message.reply("Ответьте этой командой на сообщение пользователя.")
            return

        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.reply("Эту команду нельзя применить к боту.")
            return
        if target.id == message.from_user.id:
            await message.reply("Нельзя применить эту команду к себе.")
            return

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            mode = await _command_mode(session, message.chat.id)
            if mode == "buttons" and args:
                await message.reply("Для этой группы выбран кнопочный режим. Отправьте только команду без причины/срока.")
                return
            if not await has_permission(session, message.chat.id, message.from_user.id, action):
                await message.reply("Недостаточно прав Mimorus для этой команды.")
                return

        try:
            target_member = await bot.get_chat_member(message.chat.id, target.id)
            if getattr(target_member.status, "value", str(target_member.status)) == "creator":
                await message.reply("Владельца группы нельзя наказать.")
                return
        except Exception:
            pass

        reason: str | None = None
        expires_at: datetime | None = None
        if action == "mute":
            if not args:
                await message.reply(
                    "Укажите срок мута. Формат: <code>мут 30м причина</code>, <code>мут 2ч причина</code> или <code>мут 7д причина</code>.",
                    parse_mode="HTML",
                )
                return
            delta = _duration(args[0])
            if delta is None:
                await message.reply("Не удалось определить срок. Используйте, например: 30м, 2ч или 7д.")
                return
            expires_at = datetime.now(timezone.utc) + delta
            reason = " ".join(args[1:]).strip() or None
        else:
            reason = " ".join(args).strip() or None

        await upsert_user_in_transaction(session_factory, message.from_user, target)

        target_text = _identity_from_tg(target)
        actor_text = _identity_from_tg(message.from_user)
        try:
            if action == "warning":
                async with session_factory() as session:
                    async with session.begin():
                        count = await _warning_count(session, message.chat.id, target.id) + 1
                        await _record_action(
                            session,
                            chat_id=message.chat.id,
                            target_user_id=target.id,
                            actor_user_id=message.from_user.id,
                            action="warning",
                            reason=reason,
                            warning_index=count,
                        )
                        auto_text = await _apply_warning_scale(
                            bot,
                            session,
                            chat_id=message.chat.id,
                            target_user_id=target.id,
                            actor_user_id=message.from_user.id,
                            count=count,
                            reason=reason,
                        )
                text = (
                    f"⚠️ {target_text}, Вам выдано предупреждение <b>{count}/5</b> от администратора {actor_text} за нарушение правил.\n"
                    f"Причина: <b>{escape(reason or 'не указана')}</b>.\n"
                    "Будьте аккуратнее!"
                )
                if auto_text:
                    text += "\n\n" + auto_text
                await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
                return

            if action == "mute":
                assert expires_at is not None
                await bot.restrict_chat_member(
                    message.chat.id,
                    target.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=expires_at,
                )
                async with session_factory() as session:
                    async with session.begin():
                        await _record_action(
                            session,
                            chat_id=message.chat.id,
                            target_user_id=target.id,
                            actor_user_id=message.from_user.id,
                            action="mute",
                            reason=reason,
                            expires_at=expires_at,
                        )
                await message.answer(
                    f"🔇 {target_text} получил мут до <b>{_format_expiry(expires_at)}</b>.\n"
                    f"Администратор: {actor_text}\nПричина: <b>{escape(reason or 'не указана')}</b>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            if action == "ban":
                await bot.ban_chat_member(message.chat.id, target.id)
                async with session_factory() as session:
                    async with session.begin():
                        await _record_action(
                            session,
                            chat_id=message.chat.id,
                            target_user_id=target.id,
                            actor_user_id=message.from_user.id,
                            action="ban",
                            reason=reason,
                        )
                await message.answer(
                    f"⛔ {target_text} забанен.\nАдминистратор: {actor_text}\nПричина: <b>{escape(reason or 'не указана')}</b>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            if action == "unmute":
                await bot.restrict_chat_member(message.chat.id, target.id, permissions=_unmuted_permissions())
                async with session_factory() as session:
                    async with session.begin():
                        await _deactivate(session, chat_id=message.chat.id, target_user_id=target.id, action="mute")
                        await write_audit(
                            session,
                            "moderation.unmute",
                            chat_id=message.chat.id,
                            actor_user_id=message.from_user.id,
                            target_type="user",
                            target_id=str(target.id),
                            payload={"reason": reason},
                        )
                await message.answer(f"🔊 {target_text} размучен администратором {actor_text}.", parse_mode="HTML")
                return

            if action == "unban":
                await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
                async with session_factory() as session:
                    async with session.begin():
                        await _deactivate(session, chat_id=message.chat.id, target_user_id=target.id, action="ban")
                        await write_audit(
                            session,
                            "moderation.unban",
                            chat_id=message.chat.id,
                            actor_user_id=message.from_user.id,
                            target_type="user",
                            target_id=str(target.id),
                            payload={"reason": reason},
                        )
                await message.answer(f"✅ {target_text} разбанен администратором {actor_text}.", parse_mode="HTML")
                return
        except Exception as exc:
            await message.reply(f"Не удалось выполнить действие через Telegram: {escape(str(exc))[:300]}", parse_mode="HTML")

    return router


async def upsert_user_in_transaction(session_factory, actor, target) -> None:
    async with session_factory() as session:
        async with session.begin():
            await upsert_user(session, actor)
            await upsert_user(session, target)
