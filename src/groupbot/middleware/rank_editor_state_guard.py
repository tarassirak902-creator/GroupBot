from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject


class RankEditorStateGuardMiddleware(BaseMiddleware):
    """Reject stale rank-editor callbacks that could apply another draft.

    The permission editor stores one draft per private-chat FSM context. Old
    Telegram messages can remain clickable after the owner opens another group
    or another role. In particular, the historical save handler only checked
    that a dict-shaped draft existed, so an old Save button could persist the
    current draft into a different role. Bind draft-sensitive actions to the
    exact chat/role currently present in FSM state.
    """

    _DRAFT_REQUIRED_PREFIXES = (
        "gctl:perm_save:",
    )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        raw = event.data or ""
        if not raw.startswith(self._DRAFT_REQUIRED_PREFIXES):
            return await handler(event, data)

        parts = raw.split(":")
        if len(parts) != 4:
            await event.answer("Некорректная кнопка редактора.", show_alert=True)
            return None
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except ValueError:
            await event.answer("Некорректная кнопка редактора.", show_alert=True)
            return None

        state = data.get("state")
        if not isinstance(state, FSMContext):
            await event.answer(
                "Редактор устарел. Откройте ранг заново.",
                show_alert=True,
            )
            return None
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

        return await handler(event, data)
