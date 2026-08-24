from __future__ import annotations

from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.antiflood import ACTION_LABELS, DURATION_RE, _duration_label, _duration_seconds
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.audit import write_audit


class AntiLinksState(StatesGroup):
    waiting_mute_duration = State()
    waiting_whitelist_domain = State()


def _cfg(raw: dict | None) -> dict:
    data = dict((raw or {}).get("antilinks") or {})
    return {
        "enabled": bool(data.get("enabled", False)),
        "action": str(data.get("action") or "warning"),
        "mute_duration": data.get("mute_duration"),
    }


def _normalize_domain(value: str) -> str | None:
    raw = value.strip().casefold()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").strip(".").removeprefix("www.")
    except ValueError:
        return None
    if not host or "." not in host or " " in host:
        return None
    return host


async def _save_cfg(session: AsyncSession, chat_id: int, cfg: dict) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root["antilinks"] = cfg
    settings.moderation_config = root


def _keyboard(chat_id: int, cfg: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🟢 Выключить антиссылки" if cfg["enabled"] else "⚪ Включить антиссылки", callback_data=f"al:toggle:{chat_id}")],
        [InlineKeyboardButton(text="⚖️ Действие", callback_data=f"al:action:{chat_id}")],
    ]
    if cfg.get("action") == "mute":
        rows.append([InlineKeyboardButton(text="⏳ Срок мута", callback_data=f"al:duration:{chat_id}")])
    rows.extend([
        [InlineKeyboardButton(text="✅ Белый список", callback_data=f"al:whitelist:{chat_id}")],
        [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Нужны права владельца и активный тариф.", show_alert=True); return
        settings = await _ensure_group_settings(session, chat_id)
        root = dict(settings.moderation_config or {})
        cfg = _cfg(root)
        whitelist = list(root.get("link_whitelist") or [])
    action = ACTION_LABELS.get(cfg["action"], "не задано")
    duration = f"\nСрок мута: <b>{_duration_label(cfg.get('mute_duration'))}</b>" if cfg["action"] == "mute" else ""
    text = (
        "🔗 <b>Антиссылки</b>\n\n"
        f"Статус: <b>{'✅ включены' if cfg['enabled'] else '❌ выключены'}</b>\n"
        f"Действие: <b>{action}</b>{duration}\n"
        f"Доменов в белом списке: <b>{len(whitelist)}</b>\n\n"
        "Сообщения со ссылками удаляются, если домен не находится в белом списке.\n"
        "Администрация, VIP и Недотрога защищены автоматически.\n\n"
        "При действии «Предупреждение» используется общая настраиваемая шкала предупреждений группы."
    )
    if callback.message:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_keyboard(chat_id, cfg))
    await callback.answer()


def create_antilinks_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="antilinks_settings")

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":antilinks"))
    async def open_antilinks(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear(); chat_id = int((callback.data or "").split(":", 3)[2]); await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":whitelist"))
    async def open_whitelist_from_menu(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear(); chat_id = int((callback.data or "").split(":", 3)[2]); await whitelist_screen(callback, chat_id)

    @router.callback_query(F.data.startswith("al:toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config)
                if cfg["action"] == "mute" and _duration_seconds(str(cfg.get("mute_duration") or "")) is None:
                    await callback.answer("Для мута сначала задайте срок.", show_alert=True); return
                cfg["enabled"] = not cfg["enabled"]
                await _save_cfg(session, chat_id, cfg)
                await write_audit(session, "group.antilinks_toggled", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="group", target_id=str(chat_id), payload={"enabled": cfg["enabled"]})
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("al:action:"))
    async def choose_action(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"al:set_action:{chat_id}:{key}")] for key, label in ACTION_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ Антиссылки", callback_data=f"gctl:feature:{chat_id}:antilinks")])
        if callback.message:
            await callback.message.edit_text("⚖️ <b>Действие антиссылок</b>\n\nВыберите наказание:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("al:set_action:"))
    async def set_action(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3); chat_id = int(parts[2]); action = parts[3]
        if action not in ACTION_LABELS: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): await callback.answer("Недостаточно прав.", show_alert=True); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config); cfg["action"] = action
                if action != "mute": cfg["mute_duration"] = None
                await _save_cfg(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("al:duration:"))
    async def duration(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        choices = ("15м", "30м", "1ч", "2ч", "1д", "7д")
        rows = [[InlineKeyboardButton(text=value, callback_data=f"al:set_duration:{chat_id}:{value}") for value in choices[i:i+2]] for i in range(0, len(choices), 2)]
        rows.append([InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"al:custom_duration:{chat_id}")])
        rows.append([InlineKeyboardButton(text="◀️ Антиссылки", callback_data=f"gctl:feature:{chat_id}:antilinks")])
        if callback.message: await callback.message.edit_text("⏳ <b>Срок мута</b>\n\nВыберите срок:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("al:set_duration:"))
    async def set_duration(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3); chat_id = int(parts[2]); token = parts[3]
        if not DURATION_RE.match(token) or _duration_seconds(token) is None: return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config); cfg["mute_duration"] = token; await _save_cfg(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("al:custom_duration:"))
    async def custom_duration(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2]); await state.set_state(AntiLinksState.waiting_mute_duration); await state.update_data(al_chat_id=chat_id)
        if callback.message: await callback.message.edit_text("✍️ Отправьте срок мута, например <code>45м</code>, <code>3ч</code> или <code>2д</code>.", parse_mode="HTML")
        await callback.answer()

    @router.message(AntiLinksState.waiting_mute_duration, F.chat.type == "private")
    async def save_custom_duration(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip().casefold()
        if not DURATION_RE.match(token) or _duration_seconds(token) is None: await message.answer("Не удалось определить срок."); return
        chat_id = int((await state.get_data())["al_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); cfg = _cfg(settings.moderation_config); cfg["mute_duration"] = token; await _save_cfg(session, chat_id, cfg)
        try: await message.delete()
        except Exception: pass
        await state.clear(); await message.answer("✅ Срок мута сохранён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Антиссылки", callback_data=f"gctl:feature:{chat_id}:antilinks")]]))

    async def whitelist_screen(callback: CallbackQuery, chat_id: int) -> None:
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id): await callback.answer("Недостаточно прав.", show_alert=True); return
            settings = await _ensure_group_settings(session, chat_id); domains = sorted({str(x) for x in ((settings.moderation_config or {}).get("link_whitelist") or [])})
        lines = ["✅ <b>Белый список ссылок</b>", "", "Разрешённые домены:"]
        lines.extend([f"• <code>{domain}</code>" for domain in domains] or ["• список пуст"])
        rows = [[InlineKeyboardButton(text="➕ Добавить домен", callback_data=f"al:add_domain:{chat_id}")]]
        for domain in domains[:20]: rows.append([InlineKeyboardButton(text=f"🗑 {domain}"[:64], callback_data=f"al:del_domain:{chat_id}:{domain}")])
        rows.append([InlineKeyboardButton(text="◀️ Антиссылки", callback_data=f"gctl:feature:{chat_id}:antilinks")])
        if callback.message: await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("al:whitelist:"))
    async def whitelist(callback: CallbackQuery) -> None:
        await whitelist_screen(callback, int((callback.data or "").split(":", 2)[2]))

    @router.callback_query(F.data.startswith("al:add_domain:"))
    async def add_domain(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2]); await state.set_state(AntiLinksState.waiting_whitelist_domain); await state.update_data(al_chat_id=chat_id)
        if callback.message: await callback.message.edit_text("➕ <b>Добавить домен</b>\n\nОтправьте домен или ссылку, например <code>example.com</code>.", parse_mode="HTML")
        await callback.answer()

    @router.message(AntiLinksState.waiting_whitelist_domain, F.chat.type == "private")
    async def save_domain(message: Message, state: FSMContext) -> None:
        domain = _normalize_domain(message.text or "")
        if domain is None: await message.answer("Не удалось определить домен. Пример: example.com"); return
        chat_id = int((await state.get_data())["al_chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id): await state.clear(); return
                settings = await _ensure_group_settings(session, chat_id); root = dict(settings.moderation_config or {}); domains = {str(x) for x in (root.get("link_whitelist") or [])}; domains.add(domain); root["link_whitelist"] = sorted(domains); settings.moderation_config = root
        try: await message.delete()
        except Exception: pass
        await state.clear(); await message.answer(f"✅ Домен <code>{domain}</code> добавлен.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Белый список", callback_data=f"al:whitelist:{chat_id}")]]))

    @router.callback_query(F.data.startswith("al:del_domain:"))
    async def delete_domain(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3); chat_id = int(parts[2]); domain = parts[3]
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id): return
                settings = await _ensure_group_settings(session, chat_id); root = dict(settings.moderation_config or {}); domains = {str(x) for x in (root.get("link_whitelist") or [])}; domains.discard(domain); root["link_whitelist"] = sorted(domains); settings.moderation_config = root
        await whitelist_screen(callback, chat_id)

    return router
