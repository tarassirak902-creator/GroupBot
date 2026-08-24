from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.audit import write_audit


DURATION_RE = re.compile(r"^(\d+)(м|мин|ч|д)$", re.IGNORECASE)
ACTION_LABELS = {
    "warning": "⚠️ Предупреждение",
    "mute": "🔇 Мут",
}


class AntiFloodState(StatesGroup):
    waiting_limit = State()
    waiting_window = State()
    waiting_mute_duration = State()


def _config(raw: dict | None) -> dict:
    data = dict((raw or {}).get("antiflood") or {})
    return {
        "enabled": bool(data.get("enabled", False)),
        "message_limit": data.get("message_limit"),
        "window_seconds": data.get("window_seconds"),
        "action": data.get("action"),
        "mute_duration": data.get("mute_duration"),
    }


def _duration_seconds(token: str) -> int | None:
    match = DURATION_RE.match(token.strip().casefold())
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    unit = match.group(2).casefold()
    if unit in {"м", "мин"}:
        return value * 60
    if unit == "ч":
        return value * 3600
    return value * 86400


def _window_seconds(token: str) -> int | None:
    value = token.strip().casefold()
    match = re.fullmatch(r"(\d+)(с|сек|м|мин|ч)?", value)
    if not match:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    unit = match.group(2) or "с"
    if unit in {"с", "сек"}:
        return amount
    if unit in {"м", "мин"}:
        return amount * 60
    return amount * 3600


def _window_label(seconds: int | None) -> str:
    if not seconds:
        return "не задан"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    if seconds % 60 == 0:
        return f"{seconds // 60} мин."
    return f"{seconds} сек."


def _duration_label(token: str | None) -> str:
    if not token:
        return "не задан"
    match = DURATION_RE.match(token)
    if not match:
        return escape(token)
    amount = int(match.group(1))
    unit = match.group(2).casefold()
    if unit in {"м", "мин"}:
        return f"{amount} мин."
    if unit == "ч":
        return f"{amount} ч."
    return f"{amount} дн."


def _complete(cfg: dict) -> bool:
    try:
        limit = int(cfg.get("message_limit"))
        window = int(cfg.get("window_seconds"))
    except (TypeError, ValueError):
        return False
    if limit < 2 or window <= 0 or cfg.get("action") not in ACTION_LABELS:
        return False
    if cfg.get("action") == "mute" and _duration_seconds(str(cfg.get("mute_duration") or "")) is None:
        return False
    return True


def _keyboard(chat_id: int, cfg: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🟢 Выключить антифлуд" if cfg["enabled"] else "⚪ Включить антифлуд", callback_data=f"af:toggle:{chat_id}")],
        [InlineKeyboardButton(text="💬 Количество сообщений", callback_data=f"af:set_limit:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Временной промежуток", callback_data=f"af:set_window:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"af:action:{chat_id}")],
    ]
    if cfg.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"af:set_duration:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _choice_keyboard(prefix: str, chat_id: int, choices: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(choices), 2):
        rows.append([InlineKeyboardButton(text=label, callback_data=f"af:quick:{prefix}:{chat_id}:{value}") for label, value in choices[index:index + 2]])
    rows.append([InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"af:custom:{prefix}:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _delete_user_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        cfg = _config(settings.moderation_config)
    action = ACTION_LABELS.get(str(cfg.get("action")), "не задано")
    duration_line = f"\nСрок мута: <b>{_duration_label(cfg.get('mute_duration'))}</b>" if cfg.get("action") == "mute" else ""
    text = (
        "💬 <b>Антифлуд</b>\n\n"
        f"Статус: <b>{'✅ включён' if cfg['enabled'] else '❌ выключен'}</b>\n"
        f"Количество сообщений: <b>{cfg.get('message_limit') or 'не задано'}</b>\n"
        f"Временной промежуток: <b>{_window_label(cfg.get('window_seconds'))}</b>\n"
        f"Действие: <b>{action}</b>{duration_line}\n\n"
        "Администрация, VIP и Недотрога защищены автоматически.\n\n"
        "Антифлуд срабатывает при достижении лимита сообщений в выбранном окне. "
        "Если выбрано предупреждение, дальше действует общая шкала предупреждений Mimorus."
    )
    if callback.message is not None:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_keyboard(chat_id, cfg))
    await callback.answer()


async def _save(session: AsyncSession, chat_id: int, cfg: dict) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root["antiflood"] = cfg
    settings.moderation_config = root


def create_antiflood_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="antiflood_settings")

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":antiflood"))
    async def open_antiflood(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        try:
            chat_id = int((callback.data or "").split(":", 3)[2])
        except (ValueError, IndexError):
            return
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("af:toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                if not cfg["enabled"] and not _complete(cfg):
                    await callback.answer("Сначала задайте количество сообщений, промежуток и действие. Для мута также задайте срок.", show_alert=True); return
                cfg["enabled"] = not cfg["enabled"]
                await _save(session, chat_id, cfg)
                await write_audit(session, "group.antiflood_toggled", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="group", target_id=str(chat_id), payload={"enabled": cfg["enabled"]})
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("af:set_limit:"))
    async def set_limit(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        if callback.message is not None:
            await callback.message.edit_text("💬 <b>Количество сообщений</b>\n\nВыберите лимит сообщений для срабатывания антифлуда:", parse_mode="HTML", reply_markup=_choice_keyboard("limit", chat_id, [(str(v), str(v)) for v in (3, 5, 7, 10, 15, 20)]))
        await callback.answer()

    @router.callback_query(F.data.startswith("af:set_window:"))
    async def set_window(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        choices = [("30с", "30"), ("1м", "60"), ("5м", "300"), ("15м", "900"), ("1ч", "3600"), ("5ч", "18000"), ("15ч", "54000")]
        if callback.message is not None:
            await callback.message.edit_text("⏱ <b>Временной промежуток</b>\n\nВыберите окно антифлуда:", parse_mode="HTML", reply_markup=_choice_keyboard("window", chat_id, choices))
        await callback.answer()

    @router.callback_query(F.data.startswith("af:set_duration:"))
    async def set_duration(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        choices = [("15м", "15м"), ("30м", "30м"), ("1ч", "1ч"), ("2ч", "2ч"), ("1д", "1д"), ("7д", "7д")]
        if callback.message is not None:
            await callback.message.edit_text("⏳ <b>Срок мута</b>\n\nВыберите срок:", parse_mode="HTML", reply_markup=_choice_keyboard("duration", chat_id, choices))
        await callback.answer()

    @router.callback_query(F.data.startswith("af:quick:"))
    async def quick_value(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5: return
        key, chat_raw, raw_value = parts[2], parts[3], parts[4]
        try: chat_id = int(chat_raw)
        except ValueError: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                if key == "limit":
                    value = int(raw_value)
                    if value < 2: return
                    cfg["message_limit"] = value
                elif key == "window":
                    value = int(raw_value)
                    if value <= 0: return
                    cfg["window_seconds"] = value
                elif key == "duration":
                    if _duration_seconds(raw_value) is None: return
                    cfg["mute_duration"] = raw_value
                else: return
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("af:custom:"))
    async def custom_value(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4: return
        key = parts[2]
        try: chat_id = int(parts[3])
        except ValueError: return
        state_map = {"limit": AntiFloodState.waiting_limit, "window": AntiFloodState.waiting_window, "duration": AntiFloodState.waiting_mute_duration}
        if key not in state_map: return
        await state.set_state(state_map[key]); await state.update_data(af_chat_id=chat_id)
        prompts = {"limit": "Отправьте своё количество сообщений. Минимум 2.", "window": "Отправьте свой промежуток, например <code>45с</code>, <code>20м</code> или <code>3ч</code>.", "duration": "Отправьте свой срок мута, например <code>45м</code>, <code>3ч</code> или <code>2д</code>."}
        if callback.message is not None:
            await callback.message.edit_text(f"✍️ <b>Свой вариант</b>\n\n{prompts[key]}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood")]]))
        await callback.answer()

    @router.message(AntiFloodState.waiting_limit, F.chat.type == "private")
    async def save_limit(message: Message, state: FSMContext) -> None:
        try:
            value = int((message.text or "").strip())
            if value < 2: raise ValueError
        except ValueError:
            await message.answer("Отправьте целое число не меньше 2."); return
        chat_id = int((await state.get_data())["af_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["message_limit"] = value; await _save(session, chat_id, cfg)
        await _delete_user_input(message); await state.clear()
        await message.answer(f"✅ Количество сообщений: <b>{value}</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood")]]))

    @router.message(AntiFloodState.waiting_window, F.chat.type == "private")
    async def save_window(message: Message, state: FSMContext) -> None:
        seconds = _window_seconds(message.text or "")
        if seconds is None: await message.answer("Не удалось определить промежуток. Примеры: 45с, 20м, 3ч."); return
        chat_id = int((await state.get_data())["af_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["window_seconds"] = seconds; await _save(session, chat_id, cfg)
        await _delete_user_input(message); await state.clear()
        await message.answer(f"✅ Временной промежуток: <b>{_window_label(seconds)}</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood")]]))

    @router.callback_query(F.data.startswith("af:action:"))
    async def choose_action(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"af:set_action:{chat_id}:{key}")] for key, label in ACTION_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood")])
        if callback.message is not None:
            await callback.message.edit_text("⚖️ <b>Действие антифлуда</b>\n\nВыберите наказание. При выборе предупреждения дальнейшая эскалация идёт по общей шкале Mimorus:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("af:set_action:"))
    async def set_action(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3); chat_id = int(parts[2]); action = parts[3]
        if action not in ACTION_LABELS: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["action"] = action
                if action != "mute": cfg["mute_duration"] = None
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.message(AntiFloodState.waiting_mute_duration, F.chat.type == "private")
    async def save_duration(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip().casefold()
        if _duration_seconds(token) is None: await message.answer("Не удалось определить срок. Используйте формат 45м, 3ч или 2д."); return
        chat_id = int((await state.get_data())["af_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _config(settings.moderation_config); cfg["mute_duration"] = token; await _save(session, chat_id, cfg)
        await _delete_user_input(message); await state.clear()
        await message.answer(f"✅ Срок мута: <b>{_duration_label(token)}</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood")]]))

    return router
