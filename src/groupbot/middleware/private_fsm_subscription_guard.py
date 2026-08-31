from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_owner


EXPIRED_FSM_TEXT = (
    "⚠️ Активная подписка закончилась. Текущая настройка отменена и не была сохранена. "
    "Продлите или активируйте тариф в личных сообщениях с Mimorus, затем откройте настройку заново."
)

# Owner-wide configuration flows that may not yet have a concrete chat_id in
# FSM data. Payment/tariff FSMs are intentionally not included here so users can
# still activate or renew a subscription after expiry.
OWNER_WIDE_SUBSCRIPTION_STATES = {
    "NetworkCreateState:waiting_name",
}


def _negative_chat_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value < 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed < 0 else None
    if isinstance(value, dict):
        # Prefer keys that explicitly describe a group/chat id before scanning
        # other state values such as message ids or user ids.
        for key, nested in value.items():
            key_text = str(key).casefold()
            if "chat_id" in key_text or key_text.endswith("_chat"):
                found = _negative_chat_id(nested)
                if found is not None:
                    return found
        for nested in value.values():
            found = _negative_chat_id(nested)
            if found is not None:
                return found
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            found = _negative_chat_id(nested)
            if found is not None:
                return found
    return None


class PrivateFsmSubscriptionGuardMiddleware(BaseMiddleware):
    """Cancel stale private configuration FSMs when the tariff expires mid-flow."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type != "private" or event.from_user is None:
            return await handler(event, data)

        state = data.get("state")
        if not isinstance(state, FSMContext):
            return await handler(event, data)

        state_name = await state.get_state()
        if state_name is None:
            return await handler(event, data)
        state_data = await state.get_data()
        chat_id = _negative_chat_id(state_data)
        owner_wide = state_name in OWNER_WIDE_SUBSCRIPTION_STATES
        if chat_id is None and not owner_wide:
            return await handler(event, data)

        async with self.session_factory() as session:
            if chat_id is not None:
                owner = await is_group_owner(session, chat_id, event.from_user.id)
                subscription = (
                    await active_subscription_for_owner(session, event.from_user.id)
                    if owner
                    else None
                )
            else:
                owner = True
                subscription = await active_subscription_for_owner(session, event.from_user.id)

        if chat_id is not None and not owner:
            await state.clear()
            await event.answer(
                "⚠️ Эта настройка больше недоступна: вы не являетесь владельцем выбранной группы. "
                "Откройте нужную группу в личном кабинете заново."
            )
            return None

        if subscription is None:
            await state.clear()
            await event.answer(EXPIRED_FSM_TEXT)
            return None

        return await handler(event, data)
