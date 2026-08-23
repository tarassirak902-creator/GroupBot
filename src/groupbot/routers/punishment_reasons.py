from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupSettings, Tariff
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.services.audit import write_audit
from groupbot.services.subscriptions import active_subscription_for_owner


ACTION_TITLES = {
    "warning": "⚠️ Предупреждение",
    "mute": "🔇 Мут",
    "ban": "⛔ Бан",
}
DURATION_RE = re.compile(r"^(\d+)(м|мин|ч|д)$", re.IGNORECASE)


class ReasonState(StatesGroup):
    waiting_text = State()
    waiting_mute_duration = State()


def _reason_config(config: dict | None) -> dict[str, list[dict]]:
    raw = dict((config or {}).get("punishment_reasons") or {})
    return {
        "warning": list(raw.get("warning") or []),
        "mute": list(raw.get("mute") or []),
        "ban": list(raw.get("ban") or []),
    }


def _duration_label(token: str | None) -> str:
    if not token:
        return ""
    match = DURATION_RE.match(token)
    if not match:
        return token
    value = int(match.group(1))
    unit = match.group(2).casefold()
    if unit in {"м", "мин"}:
        return f"{value} мин."
    if unit == "ч":
        return f"{value} ч."
    return f"{value} дн."


def _keyboard(chat_id: int, reasons: dict[str, list[dict]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Причина для пред", callback_data=f"preason:add:{chat_id}:warning")],
        [InlineKeyboardButton(text="➕ Причина для мута", callback_data=f"preason:add:{chat_id}:mute")],
        [InlineKeyboardButton(text="➕ Причина для бана", callback_data=f"preason:add:{chat_id}:ban")],
    ]
    for action in ("warning", "mute", "ban"):
        for index, item in enumerate(reasons[action]):
            label = str(item.get("text") or "Причина")
            duration = _duration_label(item.get("duration"))
            suffix = f" · {duration}" if duration else ""
            rows.append([
                InlineKeyboardButton(
                    text=f"🗑 {ACTION_TITLES[action]}: {label}{suffix}"[:64],
                    callback_data=f"preason:del:{chat_id}:{action}:{index}",
                )
            ])
    rows.append([InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _trial_reason_limit(session: AsyncSession, owner_id: int) -> int | None:
    subscription = await active_subscription_for_owner(session, owner_id)
    if subscription is None:
        return None
    tariff = (
        await session.execute(select(Tariff).where(Tariff.id == subscription.tariff_id))
    ).scalar_one_or_none()
    if tariff is not None and tariff.code == "TEST":
        return 3
    return None


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        settings = await _ensure_group_settings(session, chat_id)
        reasons = _reason_config(settings.moderation_config)
        limit = await _trial_reason_limit(session, callback.from_user.id)
    total = sum(len(items) for items in reasons.values())
    lines = [
        "⚖️ <b>Причины наказаний</b>",
        "",
        "Причины создаются отдельно для этой группы. Для мута причина сохраняется вместе с фиксированным сроком.",
        "",
    ]
    for action in ("warning", "mute", "ban"):
        lines.append(f"<b>{ACTION_TITLES[action]}</b>")
        if not reasons[action]:
            lines.append("• нет причин")
        else:
            for item in reasons[action]:
                duration = _duration_label(item.get("duration"))
                suffix = f" — {duration}" if duration else ""
                lines.append(f"• {escape(str(item.get('text') or 'Причина'))}{suffix}")
        lines.append("")
    if limit is not None:
        lines.append(f"TEST: <b>{total}/{limit}</b> собственных причин.")
    if callback.message is not None:
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=_keyboard(chat_id, reasons),
        )
    await callback.answer()


def create_punishment_reasons_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="punishment_reasons")

    @router.callback_query(F.data.startswith("gctl:reasons:"))
    async def reasons(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("preason:add:"))
    async def add_reason(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            action = parts[3]
        except (ValueError, IndexError):
            return
        if action not in ACTION_TITLES:
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            reasons = _reason_config(settings.moderation_config)
            limit = await _trial_reason_limit(session, callback.from_user.id)
            total = sum(len(items) for items in reasons.values())
        if limit is not None and total >= limit:
            await callback.answer(f"На TEST доступно до {limit} собственных причин.", show_alert=True)
            return
        await state.set_state(ReasonState.waiting_text)
        await state.update_data(reason_chat_id=chat_id, reason_action=action)
        if callback.message is not None:
            await callback.message.answer(
                f"Отправьте текст причины для действия «{ACTION_TITLES[action]}»."
            )
        await callback.answer()

    @router.message(ReasonState.waiting_text, F.chat.type == "private")
    async def reason_text(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        text = " ".join((message.text or "").split()).strip()
        if not 1 <= len(text) <= 200:
            await message.answer("Причина должна содержать от 1 до 200 символов.")
            return
        data = await state.get_data()
        chat_id = int(data["reason_chat_id"])
        action = str(data["reason_action"])
        if action == "mute":
            await state.set_state(ReasonState.waiting_mute_duration)
            await state.update_data(reason_text=text)
            await message.answer(
                "Теперь отправьте фиксированный срок мута: например <code>30м</code>, <code>2ч</code> или <code>7д</code>.",
                parse_mode="HTML",
            )
            return
        await _save_reason(session_factory, chat_id, message.from_user.id, action, text, None)
        await state.clear()
        await message.answer(
            f"✅ Причина «{text}» сохранена.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚖️ Причины наказаний", callback_data=f"gctl:reasons:{chat_id}")]
            ]),
        )

    @router.message(ReasonState.waiting_mute_duration, F.chat.type == "private")
    async def mute_duration(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        token = (message.text or "").strip().casefold()
        if not DURATION_RE.match(token):
            await message.answer("Не удалось определить срок. Используйте формат 30м, 2ч или 7д.")
            return
        data = await state.get_data()
        chat_id = int(data["reason_chat_id"])
        text = str(data["reason_text"])
        await _save_reason(session_factory, chat_id, message.from_user.id, "mute", text, token)
        await state.clear()
        await message.answer(
            f"✅ Причина «{text}» сохранена со сроком {_duration_label(token)}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚖️ Причины наказаний", callback_data=f"gctl:reasons:{chat_id}")]
            ]),
        )

    @router.callback_query(F.data.startswith("preason:del:"))
    async def delete_reason(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            action = parts[3]
            index = int(parts[4])
        except (ValueError, IndexError):
            return
        if action not in ACTION_TITLES:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                config = dict(settings.moderation_config or {})
                reasons = _reason_config(config)
                if not 0 <= index < len(reasons[action]):
                    await callback.answer("Причина уже удалена.", show_alert=True)
                    return
                removed = reasons[action].pop(index)
                config["punishment_reasons"] = reasons
                settings.moderation_config = config
                await write_audit(
                    session,
                    "group.punishment_reason_deleted",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="group",
                    target_id=str(chat_id),
                    payload={"action": action, "reason": removed},
                )
        await _render(callback, session_factory, chat_id)

    return router


async def _save_reason(
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    actor_user_id: int,
    action: str,
    text: str,
    duration: str | None,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            if not await _owner_access(session, chat_id, actor_user_id):
                return
            settings = await _ensure_group_settings(session, chat_id)
            config = dict(settings.moderation_config or {})
            reasons = _reason_config(config)
            reasons[action].append({"text": text, "duration": duration})
            config["punishment_reasons"] = reasons
            settings.moderation_config = config
            await write_audit(
                session,
                "group.punishment_reason_created",
                chat_id=chat_id,
                actor_user_id=actor_user_id,
                target_type="group",
                target_id=str(chat_id),
                payload={"action": action, "text": text, "duration": duration},
            )
