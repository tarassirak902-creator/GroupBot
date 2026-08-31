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
    waiting_list_name = State()
    waiting_item = State()
    waiting_delete_item = State()
    waiting_duration = State()


def _key(kind: str) -> str:
    return "blocked_words" if kind == "words" else "blocked_phrases"


def _list_limit_key(kind: str) -> str:
    return "blocked_word_lists" if kind == "words" else "blocked_phrase_lists"


def _title(kind: str) -> str:
    return "🚫 Запрещённые слова" if kind == "words" else "📝 Запрещённые фразы"


def _entry_label(kind: str) -> str:
    return "слов" if kind == "words" else "фраз"


def _normalize_list(raw: dict | None, fallback_name: str = "Основной список") -> dict:
    data = dict(raw or {})
    items = [str(x).strip() for x in (data.get("items") or []) if str(x).strip()]
    action = str(data.get("action") or "warning")
    if action not in CONTENT_ACTION_LABELS:
        action = "warning"
    return {
        "name": str(data.get("name") or fallback_name).strip()[:100] or fallback_name,
        "enabled": bool(data.get("enabled", False)),
        "items": items,
        "action": action,
        "mute_duration": data.get("mute_duration"),
    }


def _lists(raw: dict | None, kind: str) -> list[dict]:
    data = dict((raw or {}).get(_key(kind)) or {})
    raw_lists = data.get("lists")
    if isinstance(raw_lists, list):
        return [
            _normalize_list(row, f"Список {index + 1}")
            for index, row in enumerate(raw_lists)
            if isinstance(row, dict)
        ]

    legacy = _normalize_list(data)
    if legacy["items"] or data.get("enabled") or data.get("action") or data.get("mute_duration"):
        return [legacy]
    return []


async def _save_lists(session: AsyncSession, chat_id: int, kind: str, lists: list[dict]) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root[_key(kind)] = {"lists": lists}
    settings.moderation_config = root


async def _item_limit(session: AsyncSession, owner_id: int, kind: str) -> int | None:
    return await effective_limit_for_owner(session, owner_id, _key(kind))


async def _category_limit(session: AsyncSession, owner_id: int, kind: str) -> int | None:
    return await effective_limit_for_owner(session, owner_id, _list_limit_key(kind))


def _total_items(lists: list[dict]) -> int:
    return sum(len(row.get("items") or []) for row in lists)


def _category_keyboard(chat_id: int, kind: str, lists: list[dict], category_limit: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, row in enumerate(lists):
        status = "✅" if row.get("enabled") else "⚪"
        action = CONTENT_ACTION_LABELS.get(str(row.get("action") or "warning"), "⚠️ Пред")
        rows.append([
            InlineKeyboardButton(
                text=f"{status} {row.get('name') or f'Список {index + 1}'} · {action}"[:64],
                callback_data=f"cf:list:{kind}:{chat_id}:{index}",
            )
        ])
    if category_limit is None or len(lists) < category_limit:
        rows.append([InlineKeyboardButton(text="➕ Создать список", callback_data=f"cf:create_list:{kind}:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _list_keyboard(chat_id: int, kind: str, index: int, row: dict) -> InlineKeyboardMarkup:
    entry_button = "📋 Список слов" if kind == "words" else "📋 Список фраз"
    rows = [
        [InlineKeyboardButton(
            text=("🟢 Выключить" if row.get("enabled") else "⚪ Включить"),
            callback_data=f"cf:list_toggle:{kind}:{chat_id}:{index}",
        )],
        [InlineKeyboardButton(text=entry_button, callback_data=f"cf:entries:{kind}:{chat_id}:{index}")],
        [InlineKeyboardButton(text="⚖️ Наказание", callback_data=f"cf:list_action:{kind}:{chat_id}:{index}")],
    ]
    if row.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"cf:list_duration:{kind}:{chat_id}:{index}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить список", callback_data=f"cf:list_delete_confirm:{kind}:{chat_id}:{index}")])
    rows.append([InlineKeyboardButton(text=f"◀️ {_title(kind)}", callback_data=f"gctl:feature:{chat_id}:{kind}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _entries_keyboard(chat_id: int, kind: str, index: int, *, can_add: bool) -> InlineKeyboardMarkup:
    action_row: list[InlineKeyboardButton] = []
    if can_add:
        action_row.append(InlineKeyboardButton(text="➕ Добавить", callback_data=f"cf:add:{kind}:{chat_id}:{index}"))
    action_row.append(InlineKeyboardButton(text="➖ Удалить", callback_data=f"cf:remove:{kind}:{chat_id}:{index}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        action_row,
        [InlineKeyboardButton(text="◀️ К списку", callback_data=f"cf:list:{kind}:{chat_id}:{index}")],
    ])


def _category_text(kind: str, lists: list[dict], category_limit: int | None, item_limit: int | None) -> str:
    total = _total_items(lists)
    list_count = str(len(lists)) if category_limit is None else f"{len(lists)}/{category_limit}"
    item_count = str(total) if item_limit is None else f"{total}/{item_limit}"
    lines = [
        _title(kind),
        "",
        f"Списков: <b>{list_count}</b>",
        f"Всего {_entry_label(kind)}: <b>{item_count}</b>",
        "",
        "Каждый список имеет собственное наказание и собственный срок мута.",
    ]
    over_lists = category_limit is not None and len(lists) > category_limit
    over_items = item_limit is not None and total > item_limit
    if over_lists or over_items:
        lines += [
            "",
            "⚠️ <b>Использование выше лимита текущего тарифа.</b>",
            "Существующие списки и записи сохранены: их можно редактировать, выключать и удалять. "
            "Создание новых списков/записей станет доступно после уменьшения использования или повышения тарифа.",
        ]
    elif category_limit is not None and len(lists) >= category_limit:
        lines += ["", "Лимит списков текущего тарифа исчерпан. Существующие списки можно редактировать или удалять."]
    else:
        lines += ["", "Выберите существующий список или создайте новый."]
    return "\n".join(lines)


def _list_text(kind: str, row: dict, item_limit: int | None, total_items: int) -> str:
    action = CONTENT_ACTION_LABELS.get(str(row.get("action") or "warning"), "⚠️ Пред")
    duration = ""
    if row.get("action") == "mute":
        duration = f"\nСрок мута: <b>{_duration_label(row.get('mute_duration'))}</b>"
    category_total = str(total_items) if item_limit is None else f"{total_items}/{item_limit}"
    over_limit = item_limit is not None and total_items > item_limit
    limit_note = (
        "\n\n⚠️ Лимит записей текущего тарифа превышен. Удаление и редактирование доступны, добавление новых записей заблокировано."
        if over_limit
        else ""
    )
    return (
        f"{_title(kind)} · <b>{escape(str(row.get('name') or 'Список'))}</b>\n\n"
        f"Статус: <b>{'✅ включён' if row.get('enabled') else '❌ выключен'}</b>\n"
        f"Наказание: <b>{action}</b>{duration}\n"
        f"В этом списке: <b>{len(row.get('items') or [])}</b> {_entry_label(kind)}\n"
        f"Всего в категории: <b>{category_total}</b>{limit_note}"
    )


def _entries_text(kind: str, row: dict, item_limit: int | None, total_items: int, notice: str | None = None) -> str:
    title = "📋 <b>Список слов</b>" if kind == "words" else "📋 <b>Список фраз</b>"
    category_total = str(total_items) if item_limit is None else f"{total_items}/{item_limit}"
    lines = [
        title,
        f"Категория: <b>{escape(str(row.get('name') or 'Список'))}</b>",
        f"Всего в категории: <b>{category_total}</b>",
    ]
    if item_limit is not None and total_items >= item_limit:
        lines += ["", "⚠️ Добавление новых записей недоступно: достигнут лимит текущего тарифа. Удаление остаётся доступным."]
    if notice:
        lines += ["", notice]
    items = list(row.get("items") or [])
    if items:
        lines += ["", "Добавленные записи:"] + [f"• <code>{escape(str(item))}</code>" for item in items]
    else:
        lines += ["", "Список пока пуст."]
    return "\n".join(lines)


async def _load(
    session_factory: async_sessionmaker[AsyncSession], owner_id: int, chat_id: int, kind: str
) -> tuple[list[dict], int | None, int | None] | None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, owner_id):
            return None
        settings = await _ensure_group_settings(session, chat_id)
        lists = _lists(settings.moderation_config, kind)
        item_limit = await _item_limit(session, owner_id, kind)
        category_limit = await _category_limit(session, owner_id, kind)
        return lists, item_limit, category_limit


async def _render_category(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], kind: str, chat_id: int) -> None:
    loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
    if loaded is None:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    lists, item_limit, category_limit = loaded
    if callback.message:
        await callback.message.edit_text(
            _category_text(kind, lists, category_limit, item_limit),
            parse_mode="HTML",
            reply_markup=_category_keyboard(chat_id, kind, lists, category_limit),
        )
    await callback.answer()


async def _restore_entries(
    message: Message,
    state_data: dict,
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    chat_id: int,
    index: int,
    notice: str | None = None,
) -> None:
    if message.from_user is None:
        return
    loaded = await _load(session_factory, message.from_user.id, chat_id, kind)
    if loaded is None:
        return
    lists, item_limit, _ = loaded
    if not 0 <= index < len(lists):
        return
    total_items = _total_items(lists)
    text = _entries_text(kind, lists[index], item_limit, total_items, notice)
    markup = _entries_keyboard(
        chat_id,
        kind,
        index,
        can_add=item_limit is None or total_items < item_limit,
    )
    panel_id = state_data.get("cf_panel_message_id")
    if panel_id is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(panel_id),
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


async def _restore_category_after_create(
    message: Message,
    state_data: dict,
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    chat_id: int,
) -> None:
    if message.from_user is None:
        return
    loaded = await _load(session_factory, message.from_user.id, chat_id, kind)
    if loaded is None:
        return
    lists, item_limit, category_limit = loaded
    text = _category_text(kind, lists, category_limit, item_limit)
    markup = _category_keyboard(chat_id, kind, lists, category_limit)
    panel_id = state_data.get("cf_panel_message_id")
    if panel_id is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(panel_id),
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
        await _render_category(callback, session_factory, parts[3], int(parts[2]))

    @router.callback_query(F.data.startswith("cf:create_list:"))
    async def create_list(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
        if loaded is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        lists, _, category_limit = loaded
        if category_limit is not None and len(lists) >= category_limit:
            await callback.answer(f"Достигнут лимит списков: {category_limit}.", show_alert=True)
            return
        await state.set_state(ContentFilterState.waiting_list_name)
        await state.update_data(
            cf_kind=kind,
            cf_chat_id=chat_id,
            cf_panel_message_id=callback.message.message_id if callback.message else None,
        )
        if callback.message:
            await callback.message.edit_text(
                "➕ <b>Новый список</b>\n\nОтправьте название списка (1–100 символов).",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Отмена", callback_data=f"gctl:feature:{chat_id}:{kind}")
                ]]),
            )
        await callback.answer()

    @router.message(ContentFilterState.waiting_list_name, F.chat.type == "private")
    async def save_list_name(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        state_data = await state.get_data()
        kind = str(state_data["cf_kind"])
        chat_id = int(state_data["cf_chat_id"])
        name = " ".join((message.text or "").strip().split())
        if not 1 <= len(name) <= 100:
            try:
                await message.delete()
            except Exception:
                pass
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                limit = await _category_limit(session, message.from_user.id, kind)
                if limit is not None and len(lists) >= limit:
                    await state.clear()
                    return
                if name.casefold() not in {str(row.get("name") or "").casefold() for row in lists}:
                    lists.append({
                        "name": name,
                        "enabled": False,
                        "items": [],
                        "action": "warning",
                        "mute_duration": None,
                    })
                    await _save_lists(session, chat_id, kind, lists)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await _restore_category_after_create(message, state_data, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:list:"))
    async def list_card(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
        if loaded is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        lists, item_limit, _ = loaded
        if not 0 <= index < len(lists):
            await callback.answer("Список не найден.", show_alert=True)
            return
        if callback.message:
            await callback.message.edit_text(
                _list_text(kind, lists[index], item_limit, _total_items(lists)),
                parse_mode="HTML",
                reply_markup=_list_keyboard(chat_id, kind, index, lists[index]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:entries:"))
    async def entries(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
        if loaded is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        lists, item_limit, _ = loaded
        if not 0 <= index < len(lists):
            await callback.answer("Список не найден.", show_alert=True)
            return
        total_items = _total_items(lists)
        if callback.message:
            await callback.message.edit_text(
                _entries_text(kind, lists[index], item_limit, total_items),
                parse_mode="HTML",
                reply_markup=_entries_keyboard(
                    chat_id,
                    kind,
                    index,
                    can_add=item_limit is None or total_items < item_limit,
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:list_toggle:"))
    async def list_toggle(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                if not 0 <= index < len(lists):
                    return
                row = lists[index]
                if not row.get("enabled"):
                    if not row.get("items"):
                        await callback.answer("Сначала добавьте хотя бы одну запись.", show_alert=True)
                        return
                    if row.get("action") == "mute" and _duration_seconds(str(row.get("mute_duration") or "")) is None:
                        await callback.answer("Для мута сначала задайте срок.", show_alert=True)
                        return
                row["enabled"] = not bool(row.get("enabled"))
                await _save_lists(session, chat_id, kind, lists)
        callback.data = f"cf:list:{kind}:{chat_id}:{index}"
        await list_card(callback)

    @router.callback_query(F.data.startswith("cf:list_action:"))
    async def list_action(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        rows = [[InlineKeyboardButton(
            text=label,
            callback_data=f"cf:list_set_action:{kind}:{chat_id}:{index}:{key}",
        )] for key, label in CONTENT_ACTION_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data=f"cf:list:{kind}:{chat_id}:{index}")])
        if callback.message:
            await callback.message.edit_text(
                "⚖️ <b>Наказание списка</b>\n\nВыберите действие для совпадения с любой записью этого списка.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:list_set_action:"))
    async def list_set_action(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw, action_name = (callback.data or "").split(":", 5)
        chat_id = int(chat_raw)
        index = int(index_raw)
        if action_name not in CONTENT_ACTION_LABELS:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                if not 0 <= index < len(lists):
                    return
                lists[index]["action"] = action_name
                if action_name != "mute":
                    lists[index]["mute_duration"] = None
                await _save_lists(session, chat_id, kind, lists)
        callback.data = f"cf:list:{kind}:{chat_id}:{index}"
        await list_card(callback)

    @router.callback_query(F.data.startswith("cf:list_duration:"))
    async def list_duration(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        choices = ("15м", "30м", "1ч", "2ч", "1д", "7д")
        rows = [[InlineKeyboardButton(
            text=value,
            callback_data=f"cf:list_set_duration:{kind}:{chat_id}:{index}:{value}",
        ) for value in choices[start:start + 2]] for start in range(0, len(choices), 2)]
        rows.append([InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"cf:list_custom_duration:{kind}:{chat_id}:{index}")])
        rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data=f"cf:list:{kind}:{chat_id}:{index}")])
        if callback.message:
            await callback.message.edit_text("⏳ <b>Срок мута списка</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:list_set_duration:"))
    async def list_set_duration(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw, token = (callback.data or "").split(":", 5)
        chat_id = int(chat_raw)
        index = int(index_raw)
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                if not 0 <= index < len(lists):
                    return
                lists[index]["mute_duration"] = token
                await _save_lists(session, chat_id, kind, lists)
        callback.data = f"cf:list:{kind}:{chat_id}:{index}"
        await list_card(callback)

    @router.callback_query(F.data.startswith("cf:list_custom_duration:"))
    async def list_custom_duration(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        await state.set_state(ContentFilterState.waiting_duration)
        await state.update_data(cf_kind=kind, cf_chat_id=chat_id, cf_list_index=index)
        if callback.message:
            await callback.message.edit_text(
                "✍️ Отправьте срок мута, например <code>45м</code>, <code>3ч</code> или <code>2д</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cf:list:{kind}:{chat_id}:{index}")
                ]]),
            )
        await callback.answer()

    @router.message(ContentFilterState.waiting_duration, F.chat.type == "private")
    async def save_duration(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        token = (message.text or "").strip().casefold()
        data = await state.get_data()
        kind = str(data["cf_kind"])
        chat_id = int(data["cf_chat_id"])
        index = int(data["cf_list_index"])
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            try:
                await message.delete()
            except Exception:
                pass
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                if not 0 <= index < len(lists):
                    await state.clear()
                    return
                lists[index]["mute_duration"] = token
                await _save_lists(session, chat_id, kind, lists)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await message.answer(
            "✅ Срок мута сохранён.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ К списку", callback_data=f"cf:list:{kind}:{chat_id}:{index}")
            ]]),
        )

    @router.callback_query(F.data.startswith("cf:add:"))
    async def add(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
        if loaded is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        lists, item_limit, _ = loaded
        if not 0 <= index < len(lists):
            return
        if item_limit is not None and _total_items(lists) >= item_limit:
            await callback.answer(f"Достигнут лимит записей: {item_limit}.", show_alert=True)
            return
        await state.set_state(ContentFilterState.waiting_item)
        await state.update_data(
            cf_kind=kind,
            cf_chat_id=chat_id,
            cf_list_index=index,
            cf_panel_message_id=callback.message.message_id if callback.message else None,
        )
        prompt = "Отправьте одно запрещённое слово." if kind == "words" else "Отправьте запрещённую фразу."
        if callback.message:
            await callback.message.edit_text(
                f"➕ <b>Добавление</b>\n\n{prompt}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cf:entries:{kind}:{chat_id}:{index}")
                ]]),
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
        index = int(state_data["cf_list_index"])
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
                    lists = _lists(settings.moderation_config, kind)
                    if not 0 <= index < len(lists):
                        await state.clear()
                        return
                    duplicate = any(
                        item.casefold() == str(existing).casefold()
                        for row in lists
                        for existing in (row.get("items") or [])
                    )
                    if duplicate:
                        notice = "ℹ️ Такая запись уже есть в этой категории."
                    else:
                        limit = await _item_limit(session, message.from_user.id, kind)
                        if limit is not None and _total_items(lists) >= limit:
                            notice = f"⚠️ Достигнут лимит записей: {limit}."
                        else:
                            lists[index]["items"].append(item)
                            await _save_lists(session, chat_id, kind, lists)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await _restore_entries(message, state_data, session_factory, kind, chat_id, index, notice)

    @router.callback_query(F.data.startswith("cf:remove:"))
    async def remove(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
        if loaded is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        lists, _, _ = loaded
        if not 0 <= index < len(lists) or not lists[index].get("items"):
            await callback.answer("Список уже пуст.", show_alert=True)
            return
        await state.set_state(ContentFilterState.waiting_delete_item)
        await state.update_data(
            cf_kind=kind,
            cf_chat_id=chat_id,
            cf_list_index=index,
            cf_panel_message_id=callback.message.message_id if callback.message else None,
        )
        prompt = "Введите точное слово для удаления." if kind == "words" else "Введите точную фразу для удаления."
        if callback.message:
            await callback.message.edit_text(
                f"➖ <b>Удаление</b>\n\n{prompt}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cf:entries:{kind}:{chat_id}:{index}")
                ]]),
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
        index = int(state_data["cf_list_index"])
        item = " ".join((message.text or "").strip().split())
        notice: str | None = None
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                if not 0 <= index < len(lists):
                    await state.clear()
                    return
                target = next(
                    (i for i, value in enumerate(lists[index]["items"]) if str(value).casefold() == item.casefold()),
                    None,
                )
                if target is None:
                    notice = "ℹ️ Такая запись не найдена в этом списке."
                else:
                    lists[index]["items"].pop(target)
                    if not lists[index]["items"]:
                        lists[index]["enabled"] = False
                    await _save_lists(session, chat_id, kind, lists)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await _restore_entries(message, state_data, session_factory, kind, chat_id, index, notice)

    @router.callback_query(F.data.startswith("cf:list_delete_confirm:"))
    async def list_delete_confirm(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        loaded = await _load(session_factory, callback.from_user.id, chat_id, kind)
        if loaded is None:
            return
        lists, _, _ = loaded
        if not 0 <= index < len(lists):
            return
        if callback.message:
            await callback.message.edit_text(
                f"⚠️ <b>Удалить список «{escape(str(lists[index].get('name') or 'Список'))}»?</b>\n\nВсе записи этого списка будут удалены.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"cf:list_delete:{kind}:{chat_id}:{index}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cf:list:{kind}:{chat_id}:{index}")],
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:list_delete:"))
    async def list_delete(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        index = int(index_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                lists = _lists(settings.moderation_config, kind)
                if 0 <= index < len(lists):
                    lists.pop(index)
                    await _save_lists(session, chat_id, kind, lists)
        await _render_category(callback, session_factory, kind, chat_id)

    return router
