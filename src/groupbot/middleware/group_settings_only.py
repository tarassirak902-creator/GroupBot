from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


SETTINGS_ONLY_TEXT = (
    "⚙️ Настройки функций Mimorus изменяются только в личных сообщениях с ботом.\n\n"
    "Откройте нужную группу в личном кабинете и включите или выключите функцию там. "
    "После сохранения настройка сразу действует в группе."
)

# Legacy group-chat toggles are intentionally blocked. Feature state has a
# single source of truth: the selected group's private settings panel.
_LEGACY_FEATURE_TOGGLE_RE = re.compile(
    r"^\s*[+-]\s*(?:"
    r"капча|антирейд|антифлуд|антиспам|антиссылки|"
    r"запрещ[её]нные\s+слова|запрещ[её]нные\s+фразы|"
    r"слова|фразы|расписание\s+защиты"
    r")\s*$",
    re.IGNORECASE,
)


class GroupSettingsOnlyMiddleware(BaseMiddleware):
    """Prevent legacy +/- feature toggles from becoming a second settings layer."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)

        text = event.text or event.caption or ""
        if not _LEGACY_FEATURE_TOGGLE_RE.fullmatch(text):
            return await handler(event, data)

        await event.reply(SETTINGS_ONLY_TEXT)
        return None
