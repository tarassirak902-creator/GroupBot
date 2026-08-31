from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupOwner, GroupSettings
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.protection_schedule import MODULES, schedule_active, schedule_config
from groupbot.services.subscriptions import effective_limit_for_owner

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})$")
PRESETS = (("23:00–07:00", "2300", "0700"), ("00:00–08:00", "0000", "0800"), ("01:00–09:00", "0100", "0900"), ("22:00–06:00", "2200", "0600"))
DAY_LABELS = {"daily": "Каждый день", "weekdays": "Будни", "weekends": "Выходные"}
UTC_OFFSETS = (-5, 0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12)


class ScheduleState(StatesGroup):
    waiting_custom_time = State()


def _fmt_offset(value: int) -> str:
    return f"UTC{value:+d}" if value else "UTC"


def _decode_time(value: str) -> str:
    return f"{value[:2]}:{value[2:]}"


async def _save(session: AsyncSession, chat_id: int, cfg: dict) -> None:
    settings = await _ensure_group_settings(session, chat_id)
    root = dict(settings.moderation_config or {})
    root["protection_schedule"] = cfg
    settings.moderation_config = root


async def _schedule_usage(session: AsyncSession, owner_id: int) -> tuple[int, int | None]:
    configs = (
        await session.execute(
            select(GroupSettings.moderation_config)
            .join(
                GroupOwner,
                (GroupOwner.chat_id == GroupSettings.chat_id)
                & (GroupOwner.user_id == owner_id)
                & (GroupOwner.is_current.is_(True)),
            )
        )
    ).scalars().all()
    used = sum(1 for config in configs if schedule_config(config or {})["enabled"])
    limit = await effective_limit_for_owner(session, owner_id, "protection_schedules")
    return used, limit


def _main_keyboard(chat_id: int, cfg: dict, *, can_enable: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if cfg["enabled"]:
        rows.append([InlineKeyboardButton(text="🟢 Выключить расписание", callback_data=f"ps:toggle:{chat_id}")])
    elif can_enable:
        rows.append([InlineKeyboardButton(text="⚪ Включить расписание", callback_data=f"ps:toggle:{chat_id}")])
    rows.extend([
        [InlineKeyboardButton(text="🕐 Время", callback_data=f"ps:time:{chat_id}"), InlineKeyboardButton(text="📅 Дни", callback_data=f"ps:days:{chat_id}")],
        [InlineKeyboardButton(text="🌍 Часовой пояс", callback_data=f"ps:tz:{chat_id}")],
        [InlineKeyboardButton(text="🛡 Защиты по расписанию", callback_data=f"ps:modules:{chat_id}")],
        [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        root = dict(settings.moderation_config or {})
        cfg = schedule_config(root)
        active_now = schedule_active(root)
        used, limit = await _schedule_usage(session, callback.from_user.id)
    selected = [MODULES[key] for key in cfg["modules"]]
    modules_text = ", ".join(selected) if selected else "не выбраны"
    usage_text = str(used) if limit is None else f"{used}/{limit}"
    can_enable = cfg["enabled"] or limit is None or used < limit
    text = (
        "🕐 <b>Расписание защиты</b>\n\n"
        f"Статус: <b>{'✅ включено' if cfg['enabled'] else '❌ выключено'}</b>\n"
        f"Сейчас активно: <b>{'✅ да' if active_now else '❌ нет'}</b>\n"
        f"Использовано расписаний по тарифу: <b>{usage_text}</b>\n"
        f"Время: <b>{cfg['start']}–{cfg['end']}</b>\n"
        f"Дни: <b>{DAY_LABELS.get(cfg['days'], 'Каждый день')}</b>\n"
        f"Часовой пояс: <b>{_fmt_offset(cfg['utc_offset'])}</b>\n"
        f"Защиты: <b>{modules_text}</b>\n\n"
        "В выбранное время отмеченные защиты временно включаются. После окончания окна Mimorus возвращает обычные настройки группы."
    )
    if limit is not None and used > limit:
        text += (
            "\n\n⚠️ <b>Включённых расписаний больше лимита текущего тарифа.</b> "
            "Существующие расписания сохранены: их можно редактировать или выключать. "
            "Включение нового расписания станет доступно после уменьшения использования или повышения тарифа."
        )
    elif not cfg["enabled"] and limit is not None and used >= limit:
        text += (
            "\n\nЛимит расписаний текущего тарифа исчерпан. Настройки этого расписания можно подготовить, "
            "но включить его получится после выключения расписания в другой группе или повышения тарифа."
        )
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_main_keyboard(chat_id, cfg, can_enable=can_enable),
        )
    await callback.answer()


async def _modules_screen(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        cfg = schedule_config(settings.moderation_config)
    rows = [[InlineKeyboardButton(text=("✅ " if key in cfg["modules"] else "▫️ ") + label, callback_data=f"ps:module:{chat_id}:{key}")] for key, label in MODULES.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:protection_schedule")])
    if callback.message:
        await callback.message.edit_text("🛡 <b>Защиты по расписанию</b>\n\nОтметьте модули, которые Mimorus должна временно включать в выбранное время:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


def create_protection_schedule_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="protection_schedule")

    @router.callback_query(F.data.startswith("gctl:feature:") & F.data.endswith(":protection_schedule"))
    async def open_schedule(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        chat_id = int((callback.data or "").split(":", 3)[2])
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("ps:toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = schedule_config(settings.moderation_config)
                if not cfg["enabled"] and not cfg["modules"]:
                    await callback.answer("Сначала выберите хотя бы одну защиту.", show_alert=True)
                    return
                cfg["enabled"] = not cfg["enabled"]
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("ps:time:"))
    async def choose_time(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"ps:settime:{chat_id}:{start}:{end}")] for label, start, end in PRESETS]
        rows += [[InlineKeyboardButton(text="✍️ Своё расписание", callback_data=f"ps:custom:{chat_id}")], [InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:protection_schedule")]]
        if callback.message:
            await callback.message.edit_text("🕐 <b>Время защиты</b>\n\nВыберите готовое окно или задайте своё:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("ps:settime:"))
    async def set_time(callback: CallbackQuery) -> None:
        _, _, chat_raw, start_raw, end_raw = (callback.data or "").split(":", 4)
        chat_id = int(chat_raw)
        start, end = _decode_time(start_raw), _decode_time(end_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = schedule_config(settings.moderation_config)
                cfg["start"], cfg["end"] = start, end
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("ps:custom:"))
    async def custom_time(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        await state.set_state(ScheduleState.waiting_custom_time)
        await state.update_data(chat_id=chat_id)
        if callback.message:
            await callback.message.edit_text("✍️ <b>Своё расписание</b>\n\nОтправьте время в формате <code>23:30-07:30</code>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:protection_schedule")]]))
        await callback.answer()

    @router.message(ScheduleState.waiting_custom_time, F.chat.type == "private")
    async def save_custom(message: Message, state: FSMContext) -> None:
        match = TIME_RE.match((message.text or "").strip())
        if not match:
            await message.answer("Используйте формат 23:30-07:30.")
            return
        h1, m1, h2, m2 = map(int, match.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or (h1 == h2 and m1 == m2):
            await message.answer("Проверьте время: начало и конец должны отличаться.")
            return
        data = await state.get_data()
        chat_id = int(data["chat_id"])
        start, end = f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = schedule_config(settings.moderation_config)
                cfg["start"], cfg["end"] = start, end
                await _save(session, chat_id, cfg)
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await message.answer(f"✅ Расписание сохранено: <b>{start}–{end}</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🕐 Расписание защиты", callback_data=f"gctl:feature:{chat_id}:protection_schedule")]]))

    @router.callback_query(F.data.startswith("ps:days:"))
    async def days(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = [[InlineKeyboardButton(text=label, callback_data=f"ps:setdays:{chat_id}:{key}")] for key, label in DAY_LABELS.items()]
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:protection_schedule")])
        if callback.message:
            await callback.message.edit_text("📅 <b>Дни работы</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("ps:setdays:"))
    async def set_days(callback: CallbackQuery) -> None:
        _, _, chat_raw, value = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        if value not in DAY_LABELS:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = schedule_config(settings.moderation_config)
                cfg["days"] = value
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("ps:tz:"))
    async def timezone_screen(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        rows = []
        for i in range(0, len(UTC_OFFSETS), 4):
            rows.append([InlineKeyboardButton(text=_fmt_offset(v), callback_data=f"ps:settz:{chat_id}:{v}") for v in UTC_OFFSETS[i:i+4]])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:feature:{chat_id}:protection_schedule")])
        if callback.message:
            await callback.message.edit_text("🌍 <b>Часовой пояс группы</b>\n\nВыберите пояс, по которому должно работать расписание:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("ps:settz:"))
    async def set_tz(callback: CallbackQuery) -> None:
        _, _, chat_raw, value_raw = (callback.data or "").split(":", 3)
        chat_id, value = int(chat_raw), int(value_raw)
        if value not in UTC_OFFSETS:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = schedule_config(settings.moderation_config)
                cfg["utc_offset"] = value
                await _save(session, chat_id, cfg)
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("ps:modules:"))
    async def modules_screen(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        await _modules_screen(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("ps:module:"))
    async def toggle_module(callback: CallbackQuery) -> None:
        _, _, chat_raw, key = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        if key not in MODULES:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = schedule_config(settings.moderation_config)
                selected_modules = list(cfg["modules"])
                if key in selected_modules:
                    selected_modules.remove(key)
                else:
                    selected_modules.append(key)
                cfg["modules"] = selected_modules
                await _save(session, chat_id, cfg)
        await _modules_screen(callback, session_factory, chat_id)

    return router
