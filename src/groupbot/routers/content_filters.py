from __future__ import annotations

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
        [InlineKeyboardButton(
            text=("🟢 Выключить" if cfg["enabled"] else "⚪ Включить"),
            callback_data=f"cf:toggle:{kind}:{chat_id}",
        )],
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"cf:add:{kind}:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"cf:action:{kind}:{chat_id}")],
    ]
    if cfg.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"cf:duration:{kind}:{chat_id}")])
    for index, item in enumerate(cfg["items"][:20]):
        rows.append([InlineKeyboardButton(text=f"🗑 {item}"[:64], callback_data=f"cf:del:{kind}:{chat_id}:{index}")])
    rows.append([InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], kind: str, chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        cfg = _cfg(settings.moderation_config, kind)
        limit = await _item_limit(session, callback.from_user.id, kind)
    duration = f"\nСрок мута: <b>{_duration_label(cfg.get('mute_duration'))}</b>" if cfg["action"] == "mute" else ""
    action = CONTENT_ACTION_LABELS.get(cfg["action"], "не задано")
    count_label = str(len(cfg["items"])) if limit is None else f"{len(cfg['items'])}/{limit}"
    lines = [
        f"{_title(kind)}",
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
    if cfg["items"]:
        lines += ["", "Список:"] + [f"• <code>{item}</code>" for item in cfg["items"][:20]]
    if callback.message:
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_keyboard(chat_id, kind, cfg))
    await callback.answer()


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
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _cfg(settings.moderation_config, kind)
                if not cfg["items"] and not cfg["enabled"]:
                    await callback.answer("Сначала добавьте хотя бы одну запись.", show_alert=True); return
                if cfg["action"] == "mute" and _duration_seconds(str(cfg.get("mute_duration") or "")) is None:
                    await callback.answer("Для мута сначала задайте срок.", show_alert=True); return
                cfg["enabled"] = not cfg["enabled"]
                await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:add:"))
    async def add(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            cfg = _cfg(settings.moderation_config, kind)
            limit = await _item_limit(session, callback.from_user.id, kind)
        if limit is not None and len(cfg["items"]) >= limit:
            await callback.answer(f"Достигнут лимит записей: {limit}.", show_alert=True)
            return
        await state.set_state(ContentFilterState.waiting_item)
        await state.update_data(cf_kind=kind, cf_chat_id=chat_id)
        prompt = "Отправьте одно запрещённое слово." if kind == "words" else "Отправьте запрещённую фразу."
        if callback.message: await callback.message.edit_text(f"➕ <b>Добавление</b>\n\n{prompt}", parse_mode="HTML")
        await callback.answer()

    @router.message(ContentFilterState.waiting_item, F.chat.type == "private")
    async def save_item(message: Message, state: FSMContext) -> None:
        data = await state.get_data(); kind = str(data["cf_kind"]); chat_id = int(data["cf_chat_id"])
        item = " ".join((message.text or "").strip().split())
        if not item or len(item) > 200:
            await message.answer("Введите текст длиной от 1 до 200 символов."); return
        if kind == "words" and any(ch.isspace() for ch in item):
            await message.answer("Для раздела слов добавьте одно слово без пробелов. Для текста из нескольких слов используйте «Запрещённые фразы»."); return
        limit_reached = False
        duplicate = False
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config, kind)
                duplicate = item.casefold() in {x.casefold() for x in cfg["items"]}
                if not duplicate:
                    limit = await _item_limit(session, message.from_user.id, kind)
                    if limit is not None and len(cfg["items"]) >= limit:
                        limit_reached = True
                    else:
                        cfg["items"].append(item)
                        await _save(session, chat_id, kind, cfg)
        if limit_reached:
            await state.clear()
            await message.answer("Достигнут лимит записей для текущего тарифа.")
            return
        try: await message.delete()
        except Exception: pass
        await state.clear()
        result_text = "ℹ️ Такая запись уже есть." if duplicate else "✅ Добавлено."
        await message.answer(result_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=_title(kind), callback_data=f"gctl:feature:{chat_id}:{kind}")]]))

    @router.callback_query(F.data.startswith("cf:del:"))
    async def delete(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, index_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw); index = int(index_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config, kind)
                if 0 <= index < len(cfg["items"]): cfg["items"].pop(index)
                if not cfg["items"]: cfg["enabled"] = False
                await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:action:"))
    async def action(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw)
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
        _, _, kind, chat_raw, action_name = (callback.data or "").split(":", 4); chat_id = int(chat_raw)
        if action_name not in CONTENT_ACTION_LABELS: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config, kind)
                cfg["action"] = action_name
                if action_name != "mute": cfg["mute_duration"] = None
                await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:duration:"))
    async def duration(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw)
        choices = ("15м", "30м", "1ч", "2ч", "1д", "7д")
        rows = [[InlineKeyboardButton(text=v, callback_data=f"cf:set_duration:{kind}:{chat_id}:{v}") for v in choices[i:i+2]] for i in range(0, len(choices), 2)]
        rows.append([InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"cf:custom_duration:{kind}:{chat_id}")])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:{kind}")])
        if callback.message: await callback.message.edit_text("⏳ <b>Срок мута</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("cf:set_duration:"))
    async def set_duration(callback: CallbackQuery) -> None:
        _, _, kind, chat_raw, token = (callback.data or "").split(":", 4); chat_id = int(chat_raw)
        if not DURATION_RE.match(token) or _duration_seconds(token) is None: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config, kind); cfg["mute_duration"] = token; await _save(session, chat_id, kind, cfg)
        await _render(callback, session_factory, kind, chat_id)

    @router.callback_query(F.data.startswith("cf:custom_duration:"))
    async def custom_duration(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, kind, chat_raw = (callback.data or "").split(":", 3); chat_id = int(chat_raw)
        await state.set_state(ContentFilterState.waiting_duration); await state.update_data(cf_kind=kind, cf_chat_id=chat_id)
        if callback.message: await callback.message.edit_text("✍️ Отправьте срок мута, например <code>45м</code>, <code>3ч</code> или <code>2д</code>.", parse_mode="HTML")
        await callback.answer()

    @router.message(ContentFilterState.waiting_duration, F.chat.type == "private")
    async def save_duration(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip().casefold()
        if not DURATION_RE.match(token) or _duration_seconds(token) is None:
            await message.answer("Не удалось определить срок."); return
        data = await state.get_data(); kind = str(data["cf_kind"]); chat_id = int(data["cf_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config, kind); cfg["mute_duration"] = token; await _save(session, chat_id, kind, cfg)
        try: await message.delete()
        except Exception: pass
        await state.clear(); await message.answer("✅ Срок мута сохранён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=_title(kind), callback_data=f"gctl:feature:{chat_id}:{kind}")]]))

    return router
