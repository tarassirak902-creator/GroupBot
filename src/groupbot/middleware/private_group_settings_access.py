from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.group_control import _owner_access
from groupbot.routers.member_status_guard import is_regular_group_member


SETTINGS_CALLBACK_PREFIXES = (
    "af:",       # anti-flood
    "as:",       # anti-spam
    "al:",       # anti-links and whitelist
    "cf:",       # blocked words / phrases
    "entry:",    # captcha / anti-raid settings
    "ps:",       # protection schedule
    "preason:",  # punishment reasons
)
SPECIAL_PICK_PREFIX = "priv:special_pick:"
RANK_DRAFT_SAVE_PREFIX = "gctl:perm_save:"


def _settings_chat_id(data: str) -> int | None:
    """Return the group chat id embedded in a settings callback.

    Telegram group/supergroup ids are negative. Looking for the first negative
    integer keeps this middleware independent from each router's callback
    layout while avoiding user ids and numeric setting values.
    """
    for part in data.split(":"):
        try:
            value = int(part)
        except ValueError:
            continue
        if value < 0:
            return value
    return None


def _special_pick_target(data: str) -> int | None:
    parts = data.split(":", 4)
    if len(parts) != 5:
        return None
    try:
        return int(parts[4])
    except ValueError:
        return None


def _rank_draft_target(data: str) -> tuple[int, int] | None:
    parts = data.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


class PrivateGroupSettingsAccessMiddleware(BaseMiddleware):
    """Enforce owner access and validate private group-settings callbacks."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        callback_data = event.data or ""
        is_settings = callback_data.startswith(SETTINGS_CALLBACK_PREFIXES)
        is_special_pick = callback_data.startswith(SPECIAL_PICK_PREFIX)
        is_rank_draft_save = callback_data.startswith(RANK_DRAFT_SAVE_PREFIX)
        if not is_settings and not is_special_pick and not is_rank_draft_save:
            return await handler(event, data)

        if is_rank_draft_save:
            target = _rank_draft_target(callback_data)
            state = data.get("state")
            if target is None or not isinstance(state, FSMContext):
                await event.answer(
                    "Редактор устарел. Откройте нужный ранг заново.",
                    show_alert=True,
                )
                return None
            chat_id, role_id = target
            state_data = await state.get_data()
            if (
                state_data.get("permission_draft_chat_id") != chat_id
                or state_data.get("permission_draft_role_id") != role_id
                or not isinstance(state_data.get("permission_draft"), dict)
            ):
                await event.answer(
                    "Эта кнопка относится к старому редактору. Откройте нужный ранг заново.",
                    show_alert=True,
                )
                return None
        else:
            chat_id = _settings_chat_id(callback_data)
            if chat_id is None:
                await event.answer("Не удалось определить группу для этой настройки.", show_alert=True)
                return None

        async with self.session_factory() as session:
            allowed = await _owner_access(session, chat_id, event.from_user.id)

        if not allowed:
            await event.answer(
                "Настройки этой группы доступны только владельцу при активном тарифе.",
                show_alert=True,
            )
            return None

        if is_special_pick:
            target_id = _special_pick_target(callback_data)
            bot = data.get("bot")
            if target_id is None or not isinstance(bot, Bot):
                await event.answer("Не удалось проверить выбранного участника.", show_alert=True)
                return None
            try:
                regular = await is_regular_group_member(bot, chat_id, target_id)
            except Exception:
                await event.answer("Не удалось перепроверить участника группы.", show_alert=True)
                return None
            if not regular:
                await event.answer(
                    "VIP и Недотрога назначаются только обычным участникам группы.",
                    show_alert=True,
                )
                return None

        return await handler(event, data)
