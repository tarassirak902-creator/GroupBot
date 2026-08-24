from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select, update
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
ACTION_CODES = {"warning": "w", "mute": "m", "ban": "b"}
CODE_ACTIONS = {value: key for key, value in ACTION_CODES.items()}
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
    return ACTION_ALIASES.get(parts[0].casefold()), parts[1:]


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
    return status == GroupStatus.active.value and await active_subscription_for_group(session, chat_id) is not None


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


async def _moderation_config(session: AsyncSession, chat_id: int) -> dict:
    return (
        await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id == chat_id))
    ).scalar_one_or_none() or {}


async def _command_mode(session: AsyncSession, chat_id: int) -> str:
    return str((await _moderation_config(session, chat_id)).get("admin_command_mode", "both"))


def _configured_reasons(config: dict, action: str) -> list[dict]:
    data = dict(config.get("punishment_reasons") or {})
    return list(data.get(action) or [])


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


async def _deactivate(session: AsyncSession, *, chat_id: int, target_user_id: int, action: str) -> None:
    await session.execute(
        update(ModerationAction)
        .where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_user_id == target_user_id,
            ModerationAction.action == action,
            ModerationAction.is_active.is_(True),
        )
        .values(is_active=False, revoked_at=datetime.now(timezone.utc))
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
        return "мут на 15 минут 🔇"
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
        return "мут на 1 час 🔇"
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
        return "бан ⛔"
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


def _reason_keyboard(chat_id: int, target_id: int, action: str, reasons: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    code = ACTION_CODES[action]
    for index, item in enumerate(reasons[:20]):
        text = str(item.get("text") or "Причина")
        duration = str(item.get("duration") or "")
        suffix = f" · {duration}" if duration else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{text}{suffix}"[:64],
                callback_data=f"modb:{code}:{chat_id}:{target_id}:{index}",
            )
        ])
    if action in {"warning", "ban"}:
        rows.append([
            InlineKeyboardButton(
                text="Без причины",
                callback_data=f"modb:{code}:{chat_id}:{target_id}:x",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _upsert_pair(session_factory, actor, target) -> None:
    async with session_factory() as session:
        async with session.begin():
            await upsert_user(session, actor)
            await upsert_user(session, target)


async def _execute_action(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    actor,
    target,
    action: str,
    reason: str | None,
    duration_token: str | None = None,
) -> str:
    await _upsert_pair(session_factory, actor, target)
    target_text = _identity_from_tg(target)
    actor_text = _identity_from_tg(actor)
    reason_text = escape(reason or "не указана")

    if action == "warning":
        async with session_factory() as session:
            async with session.begin():
                count = min(await _warning_count(session, chat_id, target.id) + 1, 5)
                await _record_action(
                    session,
                    chat_id=chat_id,
                    target_user_id=target.id,
                    actor_user_id=actor.id,
                    action="warning",
                    reason=reason,
                    warning_index=count,
                )
                punishment = await _apply_warning_scale(
                    bot,
                    session,
                    chat_id=chat_id,
                    target_user_id=target.id,
                    actor_user_id=actor.id,
                    count=count,
                )
        icon = "⛔" if count >= 5 else "🔇" if count in {3, 4} else "⚠️"
        punishment_text = punishment or "предупреждение ⚠️"
        return (
            f"{icon} <b>Предупреждение</b>\n\n"
            f"👤 {target_text}\n"
            f"⚠️ Предупреждения: <b>{count}/5</b> {icon}\n"
            f"Наказание: <b>{punishment_text}</b> 📌\n"
            f"Причина: <b>{reason_text}</b>\n"
            f"Администратор: {actor_text}"
        )

    if action == "mute":
        if not duration_token:
            raise ValueError("Для мута не задан срок.")
        delta = _duration(duration_token)
        if delta is None:
            raise ValueError("Некорректный срок мута.")
        expires_at = datetime.now(timezone.utc) + delta
        await bot.restrict_chat_member(
            chat_id,
            target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=expires_at,
        )
        async with session_factory() as session:
            async with session.begin():
                await _record_action(
                    session,
                    chat_id=chat_id,
                    target_user_id=target.id,
                    actor_user_id=actor.id,
                    action="mute",
                    reason=reason,
                    expires_at=expires_at,
                )
                warnings = min(await _warning_count(session, chat_id, target.id), 5)
        return (
            f"🔇 <b>Мут</b>\n\n"
            f"👤 {target_text}\n"
            f"⚠️ Предупреждения: <b>{warnings}/5</b>\n"
            f"Наказание: <b>мут до {_format_expiry(expires_at)}</b> 📌\n"
            f"Причина: <b>{reason_text}</b>\n"
            f"Администратор: {actor_text}"
        )

    if action == "ban":
        await bot.ban_chat_member(chat_id, target.id)
        async with session_factory() as session:
            async with session.begin():
                await _record_action(
                    session,
                    chat_id=chat_id,
                    target_user_id=target.id,
                    actor_user_id=actor.id,
                    action="ban",
                    reason=reason,
                )
                warnings = min(await _warning_count(session, chat_id, target.id), 5)
        return (
            f"⛔ <b>Бан</b>\n\n"
            f"👤 {target_text}\n"
            f"⚠️ Предупреждения: <b>{warnings}/5</b> ⛔\n"
            f"Наказание: <b>бан</b> 📌\n"
            f"Причина: <b>{reason_text}</b>\n"
            f"Администратор: {actor_text}"
        )

    if action == "unmute":
        await bot.restrict_chat_member(chat_id, target.id, permissions=_unmuted_permissions())
        async with session_factory() as session:
            async with session.begin():
                await _deactivate(session, chat_id=chat_id, target_user_id=target.id, action="mute")
                await write_audit(
                    session,
                    "moderation.unmute",
                    chat_id=chat_id,
                    actor_user_id=actor.id,
                    target_type="user",
                    target_id=str(target.id),
                    payload={"reason": reason},
                )
        return f"✅ <b>Мут снят</b>\n\n👤 {target_text}\nАдминистратор: {actor_text}"

    if action == "unban":
        await bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
        async with session_factory() as session:
            async with session.begin():
                await _deactivate(session, chat_id=chat_id, target_user_id=target.id, action="ban")
                await write_audit(
                    session,
                    "moderation.unban",
                    chat_id=chat_id,
                    actor_user_id=actor.id,
                    target_type="user",
                    target_id=str(target.id),
                    payload={"reason": reason},
                )
        return f"✅ <b>Разбан</b>\n\n👤 {target_text}\nАдминистратор: {actor_text}"

    raise ValueError("Неизвестное действие.")


def create_manual_moderation_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="manual_moderation")

    @router.callback_query(F.data.startswith("modb:"))
    async def button_action(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            return
        action = CODE_ACTIONS.get(parts[1])
        try:
            chat_id = int(parts[2])
            target_id = int(parts[3])
        except ValueError:
            return
        index_token = parts[4]
        if action not in {"warning", "mute", "ban"} or callback.message.chat.id != chat_id:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        async with session_factory() as session:
            if not await _group_ready(session, chat_id):
                await callback.answer("Функции группы сейчас недоступны.", show_alert=True)
                return
            if not await has_permission(session, chat_id, callback.from_user.id, action):
                await callback.answer("Недостаточно прав Mimorus.", show_alert=True)
                return
            reasons = _configured_reasons(await _moderation_config(session, chat_id), action)
        reason = None
        duration_token = None
        if index_token != "x":
            try:
                item = reasons[int(index_token)]
            except (ValueError, IndexError):
                await callback.answer("Причина больше недоступна.", show_alert=True)
                return
            reason = str(item.get("text") or "").strip() or None
            duration_token = str(item.get("duration") or "").strip() or None
        if action == "mute" and not duration_token:
            await callback.answer("У этой причины не задан срок мута.", show_alert=True)
            return
        try:
            target_member = await bot.get_chat_member(chat_id, target_id)
            target = target_member.user
            status = getattr(target_member.status, "value", str(target_member.status))
            if status == "creator":
                await callback.answer("Владельца группы нельзя наказать.", show_alert=True)
                return
            text = await _execute_action(
                bot=bot,
                session_factory=session_factory,
                chat_id=chat_id,
                actor=callback.from_user,
                target=target,
                action=action,
                reason=reason,
                duration_token=duration_token,
            )
        except Exception as exc:
            await callback.answer(f"Не удалось выполнить действие: {str(exc)[:120]}", show_alert=True)
            return
        await callback.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        await callback.answer("Выполнено")

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
                users = await _load_users(session, {r.target_user_id for r in rows} | {r.actor_user_id for r in rows})
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
            for row in rows:
                target_text = clickable_user_display(users[row.target_user_id]) if row.target_user_id in users else "Пользователь"
                actor_text = clickable_user_display(users[row.actor_user_id]) if row.actor_user_id in users else "Администратор"
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
            if not await has_permission(session, message.chat.id, message.from_user.id, action):
                await message.reply("Недостаточно прав Mimorus для этой команды.")
                return
            config = await _moderation_config(session, message.chat.id)
            reasons = _configured_reasons(config, action)

        try:
            target_member = await bot.get_chat_member(message.chat.id, target.id)
            if getattr(target_member.status, "value", str(target_member.status)) == "creator":
                await message.reply("Владельца группы нельзя наказать.")
                return
        except Exception:
            pass

        if action in {"warning", "mute", "ban"} and not args and mode in {"buttons", "both"}:
            usable = reasons
            if action == "mute":
                usable = [item for item in reasons if _duration(str(item.get("duration") or "")) is not None]
                if not usable:
                    await message.reply(
                        "Для кнопочного мута сначала добавьте хотя бы одну причину с фиксированным сроком в настройках группы."
                    )
                    return
            markup = _reason_keyboard(message.chat.id, target.id, action, usable)
            if not markup.inline_keyboard:
                await message.reply("Для этого действия пока нет доступных вариантов.")
                return
            await message.reply(
                "Выберите причину наказания:" if action != "mute" else "Выберите причину и срок мута:",
                reply_markup=markup,
            )
            return

        if mode == "buttons" and args and action in {"warning", "mute", "ban"}:
            await message.reply("Для этой группы выбран кнопочный режим. Отправьте только команду без причины/срока.")
            return

        reason: str | None = None
        duration_token: str | None = None
        if action == "mute":
            if not args:
                await message.reply(
                    "Укажите срок мута. Формат: <code>мут 30м причина</code>, <code>мут 2ч причина</code> или <code>мут 7д причина</code>.",
                    parse_mode="HTML",
                )
                return
            duration_token = args[0]
            if _duration(duration_token) is None:
                await message.reply("Не удалось определить срок. Используйте, например: 30м, 2ч или 7д.")
                return
            reason = " ".join(args[1:]).strip() or None
        else:
            reason = " ".join(args).strip() or None

        try:
            text = await _execute_action(
                bot=bot,
                session_factory=session_factory,
                chat_id=message.chat.id,
                actor=message.from_user,
                target=target,
                action=action,
                reason=reason,
                duration_token=duration_token,
            )
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:
            await message.reply(f"Не удалось выполнить действие через Telegram: {escape(str(exc))[:300]}", parse_mode="HTML")

    return router
