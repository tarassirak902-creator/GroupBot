from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.antiflood import ACTION_LABELS, DURATION_RE, _duration_label, _duration_seconds
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.subscriptions import effective_limit_for_owner

CONTENT_ACTION_LABELS = {
    **ACTION_LABELS,
    "ban": "⛔ Бан",
}


class ContentFilterState(StatesGroup):
    waiting_item = State()
    waiting_delete_item = State()
    waiting_duration = State()


def _key(kind: str) -> str:
    return "blocked_words" if kind == "words" else "blocked_phrases"


def _title(kind: str) -> str:
    return "🚫 Запрещённые слова" if kind == "words" else "📝 Запрещённые фразы"


def _cfg(raw: dict | None, kind: str) -> dict:
    data = dict((raw or {}).get(_key(kind)) or {})
    items = [str(x).strip() for x in (data.get("items") or []) if str(x).strip()]
    return {
        "enabled": bool(data.get("enabled", False)),
        "items": items,
        "action": str(data.get("action") or "warning"),
        "mute_duration": data.get("mute_duration"),
    }


async def _save(session: AsyncSession, chat_id: int, kind: str, cfg: dict) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root[_key(kind)] = cfg
    settings.moderation_config = root


async def _item_limit(session: AsyncSession, owner_id: int, kind: str) -> int | None:
    return await effective_limit_for_owner(session, owner_id, _key(kind))


def _keyboard(chat_id: int, kind: str, cfg: dict) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("🟢 Выключить" if cfg["enabled"] else "⚪ Включить"),
                callback_data=f"cf:toggle:{kind}:{chat_id}",
            )
        ],
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"cf:add:{kind}:{chat_id}"),
            InlineKeyboardButton(text="➖ Удалить", callback_data=f"cf:remove:{kind}:{chat_id}"),
        ],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"cf:action:{kind}:{chat_id}")],
    ]
    if cfg.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"cf:duration:{kind}:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _screen_text(kind: str, cfg: dict, limit: int | None, notice: str | None = None) -> str:
    duration = f"\nСрок мута: <b>{_duration_label(cfg.get('mute_duration'))}</b>" if cfg["action"] == "mute" else ""
    action = CONTENT_ACTION_LABELS.get(cfg["action"], "не задано")
    count_label = str(len(cfg["items"])) if limit is None else f"{len(cfg['items'])}/{limit}"
    lines = [
        _title(kind),
        "",
        f"Статус: <b>{'✅ включено' if cfg['enabled'] else '❌ выключено'}</b>",
        f"Действие: <b>{action}</b>{duration}",
        f"Записей: <b>{count_label}</b>",
        "",
        "Администрация, VIP и Недотрога защищены автоматически.",
        "⚠️ Пред — используется общая настраиваемая шкала группы.",
        "🔇 Мут — применяется сразу на выбранный срок.",
        "⛔ Бан — применяется сразу за первое совпадение.",
    ]
    if limit is not None:
        lines.append(f"Лимит тарифа с дополнениями: <b>{limit}</b>.")
    if notice:
        lines += ["", notice]
    if cfg["items"]:
        lines += ["", "Список:"] + [f"• <code>{escape(item)}</code>" for item in cfg["items"]]
    else:
        lines += ["", "Список пока пуст."]
    return "\n".join(lines)


async def _load_screen(
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: int,
    chat_id: int,
    kind: str,
) -> tuple[dict, int | None] | None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, owner_id):
            return None
        settings = await _ensure_group_settings(session, chat_id)
        cfg = _cfg(settings.moderation_config, kind)
        limit = await _item_limit(session, owner_id, kind)
        return cfg, limit


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], kind: str, chat_id: int) -> None:
    screen = await _load_screen(session_factory, callback.from_user.id, chat_id, kind)
    if screen is None:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    cfg, limit = screen
    if callback.message:
        await callback.message.edit_text(
            _screen_text(kind, cfg, limit),
            parse_mode="HTML",
            reply_markup=_keyboard(chat_id, kind, cfg),
        )
    await callback.answer()


async def _restore_panel(
    message: Message,
    state_data: dict,
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    chat_id: int,
    notice: str | None = None,
) -> None:
    if message.from_user is None:
        return
    screen = await _load_screen(session_factory, message.from_user.id, chat_id, kind)
    if screen is None:
        return
    cfg, limit = screen
    text = _screen_text(kind, cfg, limit, notice)
    markup = _keyboard(chat_id, kind, cfg)
    panel_message_id = state_data.get("cf_panel_message_id")
    if panel_message_id is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(panel_message_id),
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


def create_content_filters_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="content_filters_settings")

    @router.callback_query(F.data.startswith("gctl:feature:") & (F.data.endswith(":words") | F.data.endswith(":phrases")))
    async def open_feature(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        parts = (callback.data or "").split(":", 3)
        await _render(callback, session_factory, parts[3], int(parts[2]))

    @router.callback_query(F.data.startswith("cf:toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _cfg(settings.moderation_config, kind)
                if not cfg["items"] and not cfg["enabled"]:
                    await callback.answer("Сначала добавьте хотя бы одну запись.", show_alert=True)
                    return
                if cfg["action"] == "mute" and _duration_seconds(str(cfg.get("mute_duration") or "")) is None:
                    await callback.answer("Для мута сначала задайте срок.", show_alert=True)
                    return
                cfg["enabled"] = not cfg["enabled"]
                await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:add:"))
    async def add(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        screen = await _load_screen(session_factory, callback.from_user.id, chat_id, kind)
        if screen is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        cfg, limit = screen
        if limit is not None and len(cfg["items"]) >= limit:
            await callback.answer(f"Достигнут лимит записей: {limit}.", show_alert=True)
            return
        await state.set_state(ContentFilterState.waiting_item)
        await state.update_data(
            cf_kind=kind,
            cf_chat_id=chat_id,
            cf_panel_message_id=callback.message.message_id if callback.message else None,
        )
        prompt = "Отправьте одно запрещённое слово." if kind == "words" else "Отправьте запрещённую фразу."
        if callback.message:
            await callback.message.edit_text(
                f"➕ <b>Добавление</b>\n\n{prompt}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data=f"gctl:feature:{chat_id}:{kind}")]]
                ),
            )
        await callback.answer()

    @router.message(ContentFilterState.waiting_item, F.chat.type == "private")
    async def save_item(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        state_data = await state.get_data()
        kind = str(state_data["cf_kind"])
        chat_id = int(state_data["cf_chat_id"])
        item = " ".join((message.text or "").strip().split())
        notice: str | None = None
        if not item or len(item) > 200:
            notice = "⚠️ Введите текст длиной от 1 до 200 символов."
        elif kind == "words" and any(ch.isspace() for ch in item):
            notice = "⚠️ В запрещённых словах можно добавить только одно слово без пробелов."
        else:
            async with session_factory() as session:
                async with session.begin():
                    if not await _owner_access(session, chat_id, message.from_user.id):
                        await state.clear()
                        return
                    settings = await _ensure_group_settings(session, chat_id)
                    cfg = _cfg(settings.moderation_config, kind)
                    if item.casefold() in {x.casefold() for x in cfg["items"]}:
                        notice = "ℹ️ Такая запись уже есть."
                    else:
                        limit = await _item_limit(session, message.from_user.id, kind)
                        if limit is not None and len(cfg["items"]) >= limit:
                            notice = f"⚠️ Достигнут лимит записей: {limit}."
                        else:
                            cfg["items"].append(item)
                            await _save(session, chat_id, kind, cfg)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await _restore_panel(message, state_data, session_factory, kind, chat_id, notice)

    @router.callback_query(F.data.startswith("cf:remove:"))
    async def remove(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        screen = await _load_screen(session_factory, callback.from_user.id, chat_id, kind)
        if screen is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        cfg, _ = screen
        if not cfg["items"]:
            await callback.answer("Список уже пуст.", show_alert=True)
            return
        await state.set_state(ContentFilterState.waiting_delete_item)
        await state.update_data(
            cf_kind=kind,
            cf_chat_id=chat_id,
            cf_panel_message_id=callback.message.message_id if callback.message else None,
        )
        prompt = "Введите точное запрещённое слово, которое нужно удалить." if kind == "words" else "Введите точную запрещённую фразу, которую нужно удалить."
        if callback.message:
            await callback.message.edit_text(
                f"➖ <b>Удаление</b>\n\n{prompt}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data=f"gctl:feature:{chat_id}:{kind}")]]
                ),
            )
        await callback.answer()

    @router.message(ContentFilterState.waiting_delete_item, F.chat.type == "private")
    async def delete_item(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        state_data = await state.get_data()
        kind = str(state_data["cf_kind"])
        chat_id = int(state_data["cf_chat_id"])
        item = " ".join((message.text or "").strip().split())
        notice: str | None = None
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _cfg(settings.moderation_config, kind)
                target_index = next((i for i, value in enumerate(cfg["items"]) if value.casefold() == item.casefold()), None)
                if target_index is None:
                    notice = "ℹ️ Такая запись не найдена."
                else:
                    cfg["items"].pop(target_index)
                    if not cfg["items"]:
                        cfg["enabled"] = False
                    await _save(session, chat_id, kind, cfg)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await _restore_panel(message, state_data, session_factory, kind, chat_id, notice)

    @router.callback_query(F.data.startswith("cf:action:"))
    async def action(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        rows = [[InlineKeyboardButton(text=label, callback_data=f"cf:set_action:{kind}:{chat_id}:{key}")] for key, label in CONTENT_ACTION_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:{kind}")])
        if callback.message:
            await callback.message.edit_text(
                "⚖️ <b>Действие</b>\n\n"
                "⚠️ Пред — по общей шкале предупреждений.\n"
                "🔇 Мут — сразу на выбранный срок.\n"
                "⛔ Бан — сразу за первое совпадение.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:set_action:"))
    async def set_action(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, action_name = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        if action_name not in CONTENT_ACTION_LABELS:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _cfg(settings.moderation_config, kind)
                cfg["action"] = action_name
                if action_name != "mute":
                    cfg["mute_duration"] = None
                await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:duration:"))
    async def duration(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        choices = ("15м", "30м", "1ч", "2ч", "1д", "7д")
        rows = [[InlineKeyboardButton(text=v, callback_data=f"cf:set_duration:{kind}:{chat_id}:{v}") for v in choices[i:i + 2]] for i in range(0, len(choices), 2)]
        rows.append([InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"cf:custom_duration:{kind}:{chat_id}")])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:{kind}")])
        if callback.message:
            await callback.message.edit_text("⏳ <b>Срок мута</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:set_duration:"))
    async def set_duration(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, token = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _cfg(settings.moderation_config, kind)
                cfg["mute_duration"] = token
                await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:custom_duration:"))
    async def custom_duration(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        await state.set_state(ContentFilterState.waiting_duration)
        await state.update_data(cf_kind=kind, cf_chat_id=chat_id)
        if callback.message:
            await callback.message.edit_text(
                "✍️ Отправьте срок мута, например <code>45м</code>, <code>3ч</code> или <code>2д</code>.",
                parse_mode="HTML",
            )
        await callback.answer()

    @router.message(ContentFilterState.waiting_duration, F.chat.type == "private")
    async def save_duration(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip().casefold()
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            await message.answer("Не удалось определить срок.")
            return
        data = await state.get_data()
        kind = str(data["cf_kind"])
        chat_id = int(data["cf_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _cfg(settings.moderation_config, kind)
                cfg["mute_duration"] = token
                await _save(session, chat_id, kind, cfg)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await message.answer(
            "✅ Срок мута сохранён.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=_title(kind), callback_data=f"gctl:feature:{chat_id}:{kind}")]]
            ),
        )

    return router
