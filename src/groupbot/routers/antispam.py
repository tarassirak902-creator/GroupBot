from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.antiflood import (
    ACTION_LABELS,
    DURATION_RE,
    _duration_label,
    _duration_seconds,
    _window_label,
    _window_seconds,
)
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.audit import write_audit


class AntiSpamState(StatesGroup):
    waiting_repeat_count = State()
    waiting_window = State()
    waiting_similarity = State()
    waiting_mute_duration = State()


REPEAT_PRESETS = (2, 3, 4, 5, 10)
WINDOW_PRESETS = ((10, "10 сек."), (30, "30 сек."), (60, "1 мин."), (300, "5 мин."), (900, "15 мин."), (3600, "1 ч."))
SIMILARITY_PRESETS = (70, 80, 90, 95, 100)
DURATION_PRESETS = (("15м", "15 мин."), ("30м", "30 мин."), ("1ч", "1 ч."), ("2ч", "2 ч."), ("1д", "1 день"), ("7д", "7 дней"))


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
        [InlineKeyboardButton(text="🔁 Количество повторов", callback_data=f"as:pick:repeats:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Временной промежуток", callback_data=f"as:pick:window:{chat_id}")],
        [InlineKeyboardButton(text="🎯 Сходство сообщений", callback_data=f"as:pick:similarity:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"as:action:{chat_id}")],
    ]
    if cfg.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"as:pick:duration:{chat_id}")])
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


def _preset_keyboard(kind: str, chat_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if kind == "repeats":
        buttons = [InlineKeyboardButton(text=str(value), callback_data=f"as:set:repeats:{chat_id}:{value}") for value in REPEAT_PRESETS]
        rows = [buttons[:3], buttons[3:]]
    elif kind == "window":
        buttons = [InlineKeyboardButton(text=label, callback_data=f"as:set:window:{chat_id}:{seconds}") for seconds, label in WINDOW_PRESETS]
        rows = [buttons[:2], buttons[2:4], buttons[4:]]
    elif kind == "similarity":
        buttons = [InlineKeyboardButton(text=f"{value}%", callback_data=f"as:set:similarity:{chat_id}:{value}") for value in SIMILARITY_PRESETS]
        rows = [buttons[:3], buttons[3:]]
    elif kind == "duration":
        buttons = [InlineKeyboardButton(text=label, callback_data=f"as:set:duration:{chat_id}:{token}") for token, label in DURATION_PRESETS]
        rows = [buttons[:2], buttons[2:4], buttons[4:]]
    rows.append([InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"as:custom:{kind}:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preset_title(kind: str) -> str:
    return {
        "repeats": "🔁 <b>Количество повторов</b>\n\nВыберите, после скольких похожих сообщений должен срабатывать антиспам:",
        "window": "⏱ <b>Временной промежуток</b>\n\nВыберите окно, в котором Mimorus будет искать похожие сообщения этого пользователя:",
        "similarity": "🎯 <b>Сходство сообщений</b>\n\nВыберите минимальный процент сходства:",
        "duration": "⏳ <b>Срок мута</b>\n\nВыберите срок наказания:",
    }[kind]


async def _delete_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _custom_saved(message: Message, text: str, chat_id: int) -> None:
    await _delete_input(message)
    await message.bot.send_message(
        message.chat.id,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")]
        ]),
    )


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
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                if not cfg["enabled"] and not _complete(cfg):
                    await callback.answer("Сначала задайте повторы, промежуток, сходство и действие. Для мута также задайте срок.", show_alert=True)
                    return
                cfg["enabled"] = not cfg["enabled"]
                await _save(session, chat_id, cfg)
                await write_audit(session, "group.antispam_toggled", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="group", target_id=str(chat_id), payload={"enabled": cfg["enabled"]})
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("as:pick:"))
    async def pick_value(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4 or parts[2] not in {"repeats", "window", "similarity", "duration"}:
            return
        kind = parts[2]
        chat_id = int(parts[3])
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text(_preset_title(kind), parse_mode="HTML", reply_markup=_preset_keyboard(kind, chat_id))
        await callback.answer()

    @router.callback_query(F.data.startswith("as:set:"))
    async def set_preset(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            return
        kind, chat_raw, value_raw = parts[2], parts[3], parts[4]
        if kind not in {"repeats", "window", "similarity", "duration"}:
            return
        chat_id = int(chat_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                if kind == "repeats":
                    cfg["repeat_count"] = int(value_raw)
                elif kind == "window":
                    cfg["window_seconds"] = int(value_raw)
                elif kind == "similarity":
                    cfg["similarity_percent"] = int(value_raw)
                else:
                    if not DURATION_RE.match(value_raw) or _duration_seconds(value_raw) is None:
                        await callback.answer("Некорректный срок.", show_alert=True)
                        return
                    cfg["mute_duration"] = value_raw
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("as:custom:"))
    async def custom_value(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            return
        kind = parts[2]
        chat_id = int(parts[3])
        state_map = {
            "repeats": AntiSpamState.waiting_repeat_count,
            "window": AntiSpamState.waiting_window,
            "similarity": AntiSpamState.waiting_similarity,
            "duration": AntiSpamState.waiting_mute_duration,
        }
        state_name = state_map.get(kind)
        if state_name is None:
            return
        await state.set_state(state_name)
        await state.update_data(as_chat_id=chat_id)
        prompt = {
            "repeats": "✍️ Отправьте своё количество повторов. Минимум 2. После сохранения ваше сообщение будет удалено.",
            "window": "✍️ Отправьте свой промежуток, например 45с, 3м или 2ч. После сохранения ваше сообщение будет удалено.",
            "similarity": "✍️ Отправьте свой процент сходства от 1 до 100. После сохранения ваше сообщение будет удалено.",
            "duration": "✍️ Отправьте свой срок мута, например 45м, 3ч или 5д. После сохранения ваше сообщение будет удалено.",
        }[kind]
        if callback.message is not None:
            await callback.message.edit_text(
                prompt,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К вариантам", callback_data=f"as:pick:{kind}:{chat_id}")],
                    [InlineKeyboardButton(text="◀️ Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")],
                ]),
            )
        await callback.answer()

    @router.message(AntiSpamState.waiting_repeat_count, F.chat.type == "private")
    async def save_custom_repeats(message: Message, state: FSMContext) -> None:
        try:
            value = int((message.text or "").strip())
            if value < 2:
                raise ValueError
        except ValueError:
            await _delete_input(message)
            await message.bot.send_message(message.chat.id, "Количество повторов должно быть целым числом не меньше 2.")
            return
        chat_id = int((await state.get_data())["as_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                cfg["repeat_count"] = value
                await _save(session, chat_id, cfg)
        await state.clear()
        await _custom_saved(message, f"✅ Количество повторов сохранено: {value}.", chat_id)

    @router.message(AntiSpamState.waiting_window, F.chat.type == "private")
    async def save_custom_window(message: Message, state: FSMContext) -> None:
        seconds = _window_seconds(message.text or "")
        if seconds is None:
            await _delete_input(message)
            await message.bot.send_message(message.chat.id, "Не удалось определить промежуток. Примеры: 45с, 3м, 2ч.")
            return
        chat_id = int((await state.get_data())["as_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                cfg["window_seconds"] = seconds
                await _save(session, chat_id, cfg)
        await state.clear()
        await _custom_saved(message, f"✅ Временной промежуток сохранён: {_window_label(seconds)}", chat_id)

    @router.message(AntiSpamState.waiting_similarity, F.chat.type == "private")
    async def save_custom_similarity(message: Message, state: FSMContext) -> None:
        try:
            value = int((message.text or "").strip().rstrip("%"))
            if not 1 <= value <= 100:
                raise ValueError
        except ValueError:
            await _delete_input(message)
            await message.bot.send_message(message.chat.id, "Процент сходства должен быть целым числом от 1 до 100.")
            return
        chat_id = int((await state.get_data())["as_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                cfg["similarity_percent"] = value
                await _save(session, chat_id, cfg)
        await state.clear()
        await _custom_saved(message, f"✅ Сходство сообщений сохранено: {value}%.", chat_id)

    @router.callback_query(F.data.startswith("as:action:"))
    async def choose_action(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"as:set_action:{chat_id}:{key}")] for key, label in ACTION_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam")])
        if callback.message is not None:
            await callback.message.edit_text("⚖️ <b>Действие антиспама</b>\n\nВыберите наказание:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("as:set_action:"))
    async def save_action(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        chat_id = int(parts[2])
        action = parts[3]
        if action not in ACTION_LABELS:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                cfg["action"] = action
                if action != "mute":
                    cfg["mute_duration"] = None
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.message(AntiSpamState.waiting_mute_duration, F.chat.type == "private")
    async def save_custom_duration(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip().casefold()
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            await _delete_input(message)
            await message.bot.send_message(message.chat.id, "Не удалось определить срок. Примеры: 45м, 3ч, 5д.")
            return
        chat_id = int((await state.get_data())["as_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                cfg["mute_duration"] = token
                await _save(session, chat_id, cfg)
        await state.clear()
        await _custom_saved(message, f"✅ Срок мута сохранён: {_duration_label(token)}", chat_id)

    @router.callback_query(F.data.startswith("as:ex:"))
    async def toggle_exclusion(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        chat_id = int(parts[2])
        key = parts[3]
        if key not in {"admins", "vip", "nedotroga"}:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _config(settings.moderation_config)
                cfg["exclusions"][key] = not cfg["exclusions"][key]
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    return router
