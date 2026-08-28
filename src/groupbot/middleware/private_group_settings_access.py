from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.group_control import _owner_access


SETTINGS_CALLBACK_PREFIXES = (
    "af:",       # anti-flood
    "as:",       # anti-spam
    "al:",       # anti-links and whitelist
    "cf:",       # blocked words / phrases
    "entry:",    # captcha / anti-raid settings
    "ps:",       # protection schedule
    "preason:",  # punishment reasons
)


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


class PrivateGroupSettingsAccessMiddleware(BaseMiddleware):
    """Enforce owner + active-subscription access for every settings callback."""

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
        if not callback_data.startswith(SETTINGS_CALLBACK_PREFIXES):
            return await handler(event, data)

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

        return await handler(event, data)
