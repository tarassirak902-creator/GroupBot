from __future__ import annotations

import asyncio
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, Group, GroupOwner, GroupSettings
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.routers.manual_moderation import _group_ready, _unmuted_permissions
from groupbot.routers.user_display import clickable_identity
from groupbot.services.protected_members import is_protected_member


CAPTCHA_TIMEOUTS = (("30с", 30), ("1м", 60), ("2м", 120), ("5м", 300))
CAPTCHA_MODES = {
    "button": "✅ Простая кнопка",
    "math": "➕ Математический пример",
    "emoji": "😀 Выбор эмодзи",
    "random": "🎲 Случайная",
}
RAID_LIMITS = (3, 5, 10, 15, 20)
RAID_WINDOWS = (("30с", 30), ("1м", 60), ("5м", 300))
RAID_DURATIONS = (("5м", 300), ("15м", 900), ("30м", 1800), ("1ч", 3600))

# (chat_id, user_id) -> (message_id, deadline, expected_answer)
_pending_captcha: dict[tuple[int, int], tuple[int, datetime, str]] = {}
_join_events: dict[int, deque[datetime]] = defaultdict(deque)
_raid_until: dict[int, datetime] = {}


def _captcha_cfg(root: dict | None) -> dict:
    raw = dict((root or {}).get("captcha") or {})
    mode = str(raw.get("mode") or "button")
    if mode not in CAPTCHA_MODES:
        mode = "button"
    return {
        "enabled": bool(raw.get("enabled", False)),
        "timeout_seconds": int(raw.get("timeout_seconds") or 60),
        "fail_action": str(raw.get("fail_action") or "kick"),
        "mode": mode,
    }


def _antiraid_cfg(root: dict | None) -> dict:
    raw = dict((root or {}).get("antiraid") or {})
    return {
        "enabled": bool(raw.get("enabled", False)),
        "join_limit": int(raw.get("join_limit") or 5),
        "window_seconds": int(raw.get("window_seconds") or 60),
        "lock_seconds": int(raw.get("lock_seconds") or 900),
        "action": str(raw.get("action") or "kick"),
    }


def _name_only(user) -> str:
    return clickable_identity(
        telegram_user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=None,
    )


def _seconds_label(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    if seconds % 60 == 0:
        return f"{seconds // 60} мин."
    return f"{seconds} сек."


async def _save_config(session: AsyncSession, chat_id: int, key: str, cfg: dict) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root[key] = cfg
    settings.moderation_config = root


async def _kick(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def _apply_entry_action(bot: Bot, chat_id: int, user_id: int, action: str) -> None:
    if action == "ban":
        await bot.ban_chat_member(chat_id, user_id)
    else:
        await _kick(bot, chat_id, user_id)


async def _captcha_timeout_task(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    deadline: datetime,
    fail_action: str,
) -> None:
    wait = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    await asyncio.sleep(wait)
    current = _pending_captcha.get((chat_id, user_id))
    if current is None or current[0] != message_id or current[1] != deadline:
        return
    _pending_captcha.pop((chat_id, user_id), None)
    try:
        await _apply_entry_action(bot, chat_id, user_id, fail_action)
    except Exception:
        pass
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def _captcha_challenge(chat_id: int, user_id: int, configured_mode: str) -> tuple[str, InlineKeyboardMarkup, str]:
    mode = configured_mode
    if mode == "random":
        mode = random.choice(["button", "math", "emoji"])

    if mode == "math":
        left = random.randint(2, 12)
        right = random.randint(2, 12)
        correct = left + right
        answers = {correct}
        while len(answers) < 4:
            answers.add(max(1, correct + random.randint(-5, 5)))
        choices = list(answers)
        random.shuffle(choices)
        rows = [[
            InlineKeyboardButton(text=str(value), callback_data=f"captcha:answer:{chat_id}:{user_id}:{value}")
            for value in choices
        ]]
        return f"Решите пример: <b>{left} + {right} = ?</b>", InlineKeyboardMarkup(inline_keyboard=rows), str(correct)

    if mode == "emoji":
        emojis = ["🐱", "🐶", "🦊", "🐼", "🐸", "🦁", "🐵", "🐰"]
        choices = random.sample(emojis, 4)
        correct = random.choice(choices)
        random.shuffle(choices)
        rows = [[
            InlineKeyboardButton(text=value, callback_data=f"captcha:answer:{chat_id}:{user_id}:{value}")
            for value in choices
        ]]
        return f"Нажмите на эмодзи <b>{correct}</b>.", InlineKeyboardMarkup(inline_keyboard=rows), correct

    return (
        "Нажмите кнопку ниже, чтобы подтвердить, что Вы не робот.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Я не робот", callback_data=f"captcha:answer:{chat_id}:{user_id}:ok")
        ]]),
        "ok",
    )


def _captcha_settings_keyboard(chat_id: int, cfg: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="🟢 Выключить капчу" if cfg["enabled"] else "⚪ Включить капчу",
            callback_data=f"entry:captcha_toggle:{chat_id}",
        )],
        [InlineKeyboardButton(text="🧩 Тип капчи", callback_data=f"entry:captcha_mode:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Время на прохождение", callback_data=f"entry:captcha_time:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Если не прошёл", callback_data=f"entry:captcha_action:{chat_id}")],
        [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _antiraid_settings_keyboard(chat_id: int, cfg: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🟢 Выключить антирейд" if cfg["enabled"] else "⚪ Включить антирейд",
            callback_data=f"entry:raid_toggle:{chat_id}",
        )],
        [InlineKeyboardButton(text="👥 Лимит входов", callback_data=f"entry:raid_limit:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Окно входов", callback_data=f"entry:raid_window:{chat_id}")],
        [InlineKeyboardButton(text="🛡 Время защиты", callback_data=f"entry:raid_duration:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"entry:raid_action:{chat_id}")],
        [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
    ])


async def _render_captcha(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        cfg = _captcha_cfg(settings.moderation_config)
    action = "бан" if cfg["fail_action"] == "ban" else "удаление из группы"
    text = (
        "🧩 <b>Капча</b>\n\n"
        f"Статус: <b>{'✅ включена' if cfg['enabled'] else '❌ выключлена'}</b>\n"
        f"Тип: <b>{CAPTCHA_MODES[cfg['mode']]}</b>\n"
        f"Время на прохождение: <b>{_seconds_label(cfg['timeout_seconds'])}</b>\n"
        f"Если не прошёл: <b>{action}</b>\n\n"
        "Новый участник временно теряет возможность писать до успешного прохождения выбранной проверки."
    )
    if callback.message:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_captcha_settings_keyboard(chat_id, cfg))
    await callback.answer()


async def _render_antiraid(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        cfg = _antiraid_cfg(settings.moderation_config)
    action = "бан" if cfg["action"] == "ban" else "удаление из группы"
    text = (
        "🚨 <b>Антирейд</b>\n\n"
        f"Статус: <b>{'✅ включён' if cfg['enabled'] else '❌ выключен'}</b>\n"
        f"Порог: <b>{cfg['join_limit']} входов</b> за <b>{_seconds_label(cfg['window_seconds'])}</b>\n"
        f"Режим защиты: <b>{_seconds_label(cfg['lock_seconds'])}</b>\n"
        f"Действие с новыми входами: <b>{action}</b>\n\n"
        "При срабатывании Mimorus уведомит группу, владельца и администраторов в личных сообщениях."
    )
    if callback.message:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_antiraid_settings_keyboard(chat_id, cfg))
    await callback.answer()


def _choice_markup(rows: list[list[InlineKeyboardButton]], back_data: str) -> InlineKeyboardMarkup:
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _raid_recipients(session: AsyncSession, chat_id: int) -> set[int]:
    owners = set((await session.execute(
        select(GroupOwner.user_id).where(GroupOwner.chat_id == chat_id, GroupOwner.is_current.is_(True))
    )).scalars().all())
    admins = set((await session.execute(
        select(AdminAssignment.user_id).where(AdminAssignment.chat_id == chat_id)
    )).scalars().all())
    return {int(x) for x in owners | admins}


async def _notify_raid(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
    count: int,
    window_seconds: int,
    lock_seconds: int,
) -> None:
    group_text = (
        "🚨 <b>Похоже, на группу идёт рейд.</b>\n\n"
        "Mimorus активировала защиту.\n\n"
        f"Зафиксировано входов: <b>{count}</b> за <b>{_seconds_label(window_seconds)}</b>.\n"
        f"Защитный режим включён на <b>{_seconds_label(lock_seconds)}</b>."
    )
    try:
        await bot.send_message(chat_id, group_text, parse_mode="HTML")
    except Exception:
        pass

    async with session_factory() as session:
        recipients = await _raid_recipients(session, chat_id)
        title = (await session.execute(select(Group.title).where(Group.chat_id == chat_id))).scalar_one_or_none() or str(chat_id)

    private_text = (
        "🚨 <b>Антирейд Mimorus</b>\n\n"
        f"В группе <b>{title}</b> обнаружен массовый вход участников.\n"
        "Mimorus автоматически активировала защиту.\n\n"
        f"Входов: <b>{count}</b> за <b>{_seconds_label(window_seconds)}</b>.\n"
        f"Защита активна: <b>{_seconds_label(lock_seconds)}</b>."
    )
    for user_id in recipients:
        try:
            await bot.send_message(user_id, private_text, parse_mode="HTML")
        except Exception:
            pass


def create_entry_protection_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="entry_protection")

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":captcha"))
    async def captcha_screen(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 3)[2])
        await _render_captcha(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":antiraid"))
    async def raid_screen(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 3)[2])
        await _render_antiraid(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:captcha_toggle:"))
    async def captcha_toggle(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _captcha_cfg(settings.moderation_config)
                cfg["enabled"] = not cfg["enabled"]; await _save_config(session, chat_id, "captcha", cfg)
        await _render_captcha(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:captcha_mode:"))
    async def captcha_mode(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"entry:set_captcha_mode:{chat_id}:{mode}")] for mode, label in CAPTCHA_MODES.items()]
        if callback.message:
            await callback.message.edit_text("🧩 <b>Тип капчи</b>\n\nВыберите проверку для новых участников:", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:captcha"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_captcha_mode:"))
    async def set_captcha_mode(callback: CallbackQuery) -> None:
        _, _, chat_raw, mode = (callback.data or "").split(":", 3); chat_id = int(chat_raw)
        if mode not in CAPTCHA_MODES: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _captcha_cfg(settings.moderation_config); cfg["mode"] = mode; await _save_config(session, chat_id, "captcha", cfg)
        await _render_captcha(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:captcha_time:"))
    async def captcha_time(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"entry:set_captcha_time:{chat_id}:{seconds}") for label, seconds in CAPTCHA_TIMEOUTS[i:i+2]] for i in range(0, len(CAPTCHA_TIMEOUTS), 2)]
        if callback.message: await callback.message.edit_text("⏱ <b>Время на капчу</b>", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:captcha"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_captcha_time:"))
    async def set_captcha_time(callback: CallbackQuery) -> None:
        _, _, chat_raw, seconds_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw); seconds = int(seconds_raw)
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id); cfg = _captcha_cfg(settings.moderation_config); cfg["timeout_seconds"] = seconds; await _save_config(session, chat_id, "captcha", cfg)
        await _render_captcha(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:captcha_action:"))
    async def captcha_action(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text="🚪 Удалить из группы", callback_data=f"entry:set_captcha_action:{chat_id}:kick")], [InlineKeyboardButton(text="⛔ Бан", callback_data=f"entry:set_captcha_action:{chat_id}:ban")]]
        if callback.message: await callback.message.edit_text("⚖️ <b>Если капча не пройдена</b>", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:captcha"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_captcha_action:"))
    async def set_captcha_action(callback: CallbackQuery) -> None:
        _, _, chat_raw, action = (callback.data or "").split(":", 3); chat_id = int(chat_raw)
        if action not in {"kick", "ban"}: return
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id); cfg = _captcha_cfg(settings.moderation_config); cfg["fail_action"] = action; await _save_config(session, chat_id, "captcha", cfg)
        await _render_captcha(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:raid_toggle:"))
    async def raid_toggle(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _antiraid_cfg(settings.moderation_config); cfg["enabled"] = not cfg["enabled"]; await _save_config(session, chat_id, "antiraid", cfg)
        await _render_antiraid(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:raid_limit:"))
    async def raid_limit(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=str(v), callback_data=f"entry:set_raid_limit:{chat_id}:{v}") for v in RAID_LIMITS[i:i+3]] for i in range(0, len(RAID_LIMITS), 3)]
        if callback.message: await callback.message.edit_text("👥 <b>Лимит массового входа</b>", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:antiraid"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_raid_limit:"))
    async def set_raid_limit(callback: CallbackQuery) -> None:
        _, _, chat_raw, value_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw); value = int(value_raw)
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id); cfg = _antiraid_cfg(settings.moderation_config); cfg["join_limit"] = value; await _save_config(session, chat_id, "antiraid", cfg)
        await _render_antiraid(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:raid_window:"))
    async def raid_window(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"entry:set_raid_window:{chat_id}:{seconds}") for label, seconds in RAID_WINDOWS]]
        if callback.message: await callback.message.edit_text("⏱ <b>Окно массового входа</b>", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:antiraid"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_raid_window:"))
    async def set_raid_window(callback: CallbackQuery) -> None:
        _, _, chat_raw, value_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw); value = int(value_raw)
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id); cfg = _antiraid_cfg(settings.moderation_config); cfg["window_seconds"] = value; await _save_config(session, chat_id, "antiraid", cfg)
        await _render_antiraid(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:raid_duration:"))
    async def raid_duration(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"entry:set_raid_duration:{chat_id}:{seconds}") for label, seconds in RAID_DURATIONS[i:i+2]] for i in range(0, len(RAID_DURATIONS), 2)]
        if callback.message: await callback.message.edit_text("🛡 <b>Время защитного режима</b>", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:antiraid"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_raid_duration:"))
    async def set_raid_duration(callback: CallbackQuery) -> None:
        _, _, chat_raw, value_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw); value = int(value_raw)
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id); cfg = _antiraid_cfg(settings.moderation_config); cfg["lock_seconds"] = value; await _save_config(session, chat_id, "antiraid", cfg)
        await _render_antiraid(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("entry:raid_action:"))
    async def raid_action(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text="🚪 Удалять новых", callback_data=f"entry:set_raid_action:{chat_id}:kick")], [InlineKeyboardButton(text="⛔ Банить новых", callback_data=f"entry:set_raid_action:{chat_id}:ban")]]
        if callback.message: await callback.message.edit_text("⚖️ <b>Действие антирейда</b>", parse_mode="HTML", reply_markup=_choice_markup(rows, f"gctl:feature:{chat_id}:antiraid"))
        await callback.answer()

    @router.callback_query(F.data.startswith("entry:set_raid_action:"))
    async def set_raid_action(callback: CallbackQuery) -> None:
        _, _, chat_raw, action = (callback.data or "").split(":", 3); chat_id = int(chat_raw)
        if action not in {"kick", "ban"}: return
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id); cfg = _antiraid_cfg(settings.moderation_config); cfg["action"] = action; await _save_config(session, chat_id, "antiraid", cfg)
        await _render_antiraid(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("captcha:answer:"))
    async def captcha_answer(callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5: return
        chat_id = int(parts[2]); user_id = int(parts[3]); answer = parts[4]
        if callback.from_user.id != user_id:
            await callback.answer("Эта капча предназначена другому участнику.", show_alert=True); return
        pending = _pending_captcha.get((chat_id, user_id))
        if pending is None:
            await callback.answer("Капча уже недействительна.", show_alert=True); return
        if answer != pending[2]:
            await callback.answer("❌ Неверный ответ. Попробуйте ещё раз.", show_alert=True); return
        _pending_captcha.pop((chat_id, user_id), None)
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions=_unmuted_permissions())
        except Exception:
            await callback.answer("Не удалось снять ограничения. Проверьте права бота.", show_alert=True); return
        if callback.message:
            try: await callback.message.delete()
            except Exception: pass
        await callback.answer("✅ Проверка пройдена.")

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
    async def new_members(message: Message, bot: Bot) -> None:
        if not message.new_chat_members:
            return
        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id): return
            root = (await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id == message.chat.id))).scalar_one_or_none() or {}
            captcha = _captcha_cfg(root); raid = _antiraid_cfg(root)

        now = datetime.now(timezone.utc)
        raid_active = bool(_raid_until.get(message.chat.id) and _raid_until[message.chat.id] > now)
        if raid["enabled"]:
            events = _join_events[message.chat.id]
            cutoff = now - timedelta(seconds=raid["window_seconds"])
            while events and events[0] < cutoff: events.popleft()
            for user in message.new_chat_members:
                if not user.is_bot: events.append(now)
            if len(events) >= raid["join_limit"] and not raid_active:
                raid_active = True
                _raid_until[message.chat.id] = now + timedelta(seconds=raid["lock_seconds"])
                await _notify_raid(bot, session_factory, chat_id=message.chat.id, count=len(events), window_seconds=raid["window_seconds"], lock_seconds=raid["lock_seconds"])

        for user in message.new_chat_members:
            if user.is_bot: continue
            async with session_factory() as session:
                if await is_protected_member(session, chat_id=message.chat.id, user_id=user.id, moderation_config=root):
                    continue
            if raid_active and raid["enabled"]:
                try: await _apply_entry_action(bot, message.chat.id, user.id, raid["action"])
                except Exception: pass
                continue
            if not captcha["enabled"]:
                continue
            try:
                await bot.restrict_chat_member(message.chat.id, user.id, permissions=ChatPermissions(can_send_messages=False))
                challenge_text, markup, expected = _captcha_challenge(message.chat.id, user.id, captcha["mode"])
                challenge = await bot.send_message(
                    message.chat.id,
                    f"🧩 {_name_only(user)}, пройдите проверку.\n\n{challenge_text}\n\nНа прохождение: <b>{_seconds_label(captcha['timeout_seconds'])}</b>.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
                deadline = datetime.now(timezone.utc) + timedelta(seconds=captcha["timeout_seconds"])
                _pending_captcha[(message.chat.id, user.id)] = (challenge.message_id, deadline, expected)
                asyncio.create_task(_captcha_timeout_task(bot, chat_id=message.chat.id, user_id=user.id, message_id=challenge.message_id, deadline=deadline, fail_action=captcha["fail_action"]))
            except Exception:
                pass

    return router
