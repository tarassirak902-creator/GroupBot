from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.antiflood import ACTION_LABELS, DURATION_RE, _duration_label, _duration_seconds, _window_label, _window_seconds
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.audit import write_audit


class AntiSpamState(StatesGroup):
    waiting_repeat_count = State()
    waiting_window = State()
    waiting_similarity = State()
    waiting_mute_duration = State()


def _config(raw: dict | None) -> dict:
    data = dict((raw or {}).get("antispam") or {})
    exclusions = dict(data.get("exclusions") or {})
    return {
        "enabled": bool(data.get("enabled", False)),
        "repeat_count": data.get("repeat_count"),
        "window_seconds": data.get("window_seconds"),
        "similarity_percent": data.get("similarity_percent"),
        "action": data.get("action"),
        "mute_duration": data.get("mute_duration"),
        "exclusions": {
            "admins": bool(exclusions.get("admins", False)),
            "vip": bool(exclusions.get("vip", False)),
            "nedotroga": bool(exclusions.get("nedotroga", False)),
        },
    }


def _complete(cfg: dict) -> bool:
    try:
        repeats = int(cfg.get("repeat_count"))
        window = int(cfg.get("window_seconds"))
        similarity = int(cfg.get("similarity_percent"))
    except (TypeError, ValueError):
        return False
    if repeats < 2 or window <= 0 or not 1 <= similarity <= 100 or cfg.get("action") not in ACTION_LABELS:
        return False
    if cfg.get("action") == "mute" and _duration_seconds(str(cfg.get("mute_duration") or "")) is None:
        return False
    return True


def _keyboard(chat_id: int, cfg: dict) -> InlineKeyboardMarkup:
    ex = cfg["exclusions"]
    rows = [
        [InlineKeyboardButton(text="🟢 Выключить антиспам" if cfg["enabled"] else "⚪ Включить антиспам", callback_data=f"as:toggle:{chat_id}")],
        [InlineKeyboardButton(text="🔁 Количество повторов", callback_data=f"as:set_repeats:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Временной промежуток", callback_data=f"as:set_window:{chat_id}")],
        [InlineKeyboardButton(text="🎯 Сходство сообщений", callback_data=f"as:set_similarity:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"as:action:{chat_id}")],
    ]
    if cfg.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"as:set_duration:{chat_id}")])
    rows.extend([
        [InlineKeyboardButton(text=("✅ " if ex["admins"] else "❌ ") + "Исключать администрацию", callback_data=f"as:ex:{chat_id}:admins")],
        [InlineKeyboardButton(text=("✅ " if ex["vip"] else "❌ ") + "Исключать VIP", callback_data=f"as:ex:{chat_id}:vip")],
        [InlineKeyboardButton(text=("✅ " if ex["nedotroga"] else "❌ ") + "Исключать Недотрогу", callback_data=f"as:ex:{chat_id}:nedotroga")],
        [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _save(session: AsyncSession, chat_id: int, cfg: dict) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root["antispam"] = cfg
    settings.moderation_config = root


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        cfg = _config(settings.moderation_config)
    action = ACTION_LABELS.get(str(cfg.get("action")), "не задано")
    duration = f"\nСрок мута: <b>{_duration_label(cfg.get('mute_duration'))}</b>" if cfg.get("action") == "mute" else ""
    ex = cfg["exclusions"]
    text = (
        "🔁 <b>Антиспам</b>\n\n"
        f"Статус: <b>{'✅ включён' if cfg['enabled'] else '❌ выключен'}</b>\n"
        f"Количество похожих сообщений: <b>{cfg.get('repeat_count') or 'не задано'}</b>\n"
        f"Временной промежуток: <b>{_window_label(cfg.get('window_seconds'))}</b>\n"
        f"Минимальное сходство: <b>{str(cfg.get('similarity_percent')) + '%' if cfg.get('similarity_percent') else 'не задано'}</b>\n"
        f"Действие: <b>{action}</b>{duration}\n\n"
        "Исключения:\n"
        f"• Администрация: <b>{'да' if ex['admins'] else 'нет'}</b>\n"
        f"• VIP: <b>{'да' if ex['vip'] else 'нет'}</b>\n"
        f"• Недотрога: <b>{'да' if ex['nedotroga'] else 'нет'}</b>\n\n"
        "Сходство задаёт владелец: 100% означает только одинаковые нормализованные сообщения; меньшее значение позволяет ловить близкие варианты."
    )
    if callback.message is not None:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_keyboard(chat_id, cfg))
    await callback.answer()


def create_antispam_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="antispam_settings")

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":antispam"))
    async def open_antispam(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        try:
            chat_id = int((callback.data or "").split(":", 3)[2])
        except (ValueError, IndexError):
            return
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("as:toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                if not cfg["enabled"] and not _complete(cfg):
                    await callback.answer("Сначала задайте повторы, промежуток, сходство и действие. Для мута также задайте срок.", show_alert=True); return
                cfg["enabled"] = not cfg["enabled"]
                await _save(session, chat_id, cfg)
                await write_audit(session, "group.antispam_toggled", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="group", target_id=str(chat_id), payload={"enabled": cfg["enabled"]})
        await _render(callback, session_factory, chat_id)

    async def start_number(callback: CallbackQuery, state: FSMContext, state_name, prompt: str, key: str) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        await state.set_state(state_name)
        await state.update_data(as_chat_id=chat_id, as_key=key)
        if callback.message is not None:
            await callback.message.answer(prompt, parse_mode="HTML")
        await callback.answer()

    @router.callback_query(F.data.startswith("as:set_repeats:"))
    async def set_repeats(callback: CallbackQuery, state: FSMContext) -> None:
        await start_number(callback, state, AntiSpamState.waiting_repeat_count, "Отправьте количество похожих сообщений для срабатывания. Минимум 2.", "repeat_count")

    @router.callback_query(F.data.startswith("as:set_similarity:"))
    async def set_similarity(callback: CallbackQuery, state: FSMContext) -> None:
        await start_number(callback, state, AntiSpamState.waiting_similarity, "Отправьте минимальный процент сходства от <code>1</code> до <code>100</code>.", "similarity_percent")

    @router.message(AntiSpamState.waiting_repeat_count, F.chat.type == "private")
    @router.message(AntiSpamState.waiting_similarity, F.chat.type == "private")
    async def save_number(message: Message, state: FSMContext) -> None:
        data = await state.get_data(); key = str(data["as_key"]); chat_id = int(data["as_chat_id"])
        try:
            value = int((message.text or "").strip())
        except ValueError:
            await message.answer("Нужно целое число."); return
        if key == "repeat_count" and value < 2:
            await message.answer("Количество повторов должно быть не меньше 2."); return
        if key == "similarity_percent" and not 1 <= value <= 100:
            await message.answer("Процент сходства должен быть от 1 до 100."); return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg[key] = value; await _save(session, chat_id, cfg)
        await state.clear()
        await message.answer("✅ Настройка сохранена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")]]))

    @router.callback_query(F.data.startswith("as:set_window:"))
    async def set_window(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2]); await state.set_state(AntiSpamState.waiting_window); await state.update_data(as_chat_id=chat_id)
        if callback.message is not None:
            await callback.message.answer("Отправьте временной промежуток, например <code>30с</code>, <code>2м</code> или <code>1ч</code>.", parse_mode="HTML")
        await callback.answer()

    @router.message(AntiSpamState.waiting_window, F.chat.type == "private")
    async def save_window(message: Message, state: FSMContext) -> None:
        seconds = _window_seconds(message.text or "")
        if seconds is None:
            await message.answer("Не удалось определить промежуток. Примеры: 30с, 2м, 1ч."); return
        chat_id = int((await state.get_data())["as_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["window_seconds"] = seconds; await _save(session, chat_id, cfg)
        await state.clear(); await message.answer("✅ Временной промежуток сохранён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")]]))

    @router.callback_query(F.data.startswith("as:action:"))
    async def choose_action(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"as:set_action:{chat_id}:{key}")] for key, label in ACTION_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")])
        if callback.message is not None: await callback.message.edit_text("⚖️ <b>Действие антиспама</b>\n\nВыберите наказание:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("as:set_action:"))
    async def save_action(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3); chat_id = int(parts[2]); action = parts[3]
        if action not in ACTION_LABELS: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["action"] = action
                if action != "mute": cfg["mute_duration"] = None
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("as:set_duration:"))
    async def set_duration(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2]); await state.set_state(AntiSpamState.waiting_mute_duration); await state.update_data(as_chat_id=chat_id)
        if callback.message is not None: await callback.message.answer("Отправьте срок мута: например <code>30м</code>, <code>2ч</code> или <code>7д</code>.", parse_mode="HTML")
        await callback.answer()

    @router.message(AntiSpamState.waiting_mute_duration, F.chat.type == "private")
    async def save_duration(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip().casefold()
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            await message.answer("Не удалось определить срок. Примеры: 30м, 2ч, 7д."); return
        chat_id = int((await state.get_data())["as_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["mute_duration"] = token; await _save(session, chat_id, cfg)
        await state.clear(); await message.answer("✅ Срок мута сохранён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")]]))

    @router.callback_query(F.data.startswith("as:ex:"))
    async def toggle_exclusion(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3); chat_id = int(parts[2]); key = parts[3]
        if key not in {"admins", "vip", "nedotroga"}: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["exclusions"][key] = not cfg["exclusions"][key]; await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    return router
