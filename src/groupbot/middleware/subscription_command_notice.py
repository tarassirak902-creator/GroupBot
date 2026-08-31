from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupSettings, GroupStatus
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import (
    active_subscription_for_group,
    active_subscription_for_owner,
    effective_limit_for_owner,
)


EXPIRED_SUBSCRIPTION_TEXT = (
    "⚠️ <b>Mimorus не может выполнить это действие.</b>\n\n"
    "У владельца группы закончилась активная подписка. "
    "Команды и функции Mimorus в этой группе временно недоступны.\n\n"
    "Владельцу нужно продлить или активировать тариф в личных сообщениях с ботом."
)

EXPIRED_PRIVATE_FSM_TEXT = (
    "⚠️ Активная подписка закончилась. Текущая настройка отменена и не была сохранена. "
    "Продлите или активируйте тариф, затем откройте эту настройку заново."
)

OWNER_WIDE_SUBSCRIPTION_STATES = {
    "NetworkCreateState:waiting_name",
}

_START_CONNECT_RE = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?\s+connect\s*$",
    re.IGNORECASE,
)

_EXACT_COMMANDS = {
    "кто я", "кто он", "кто она", "кто ты",
    "топ админов", "топ администрации",
    "инфо", "информация", "профиль", "статистика", "стата",
    "нарушение",
    "банлист", "мутлист", "преды", "мои баны", "мои муты", "выдал пред", "сбанлист",
    "админы", "администрация",
    "очистить пользователя",
    "закрепи", "открепи",
    "брак", "браки", "мой брак", "развод", "развестись",
}

_COMMAND_PREFIXES = (
    "пред ", "варн ", "мут ", "бан ", "размут ", "разбан ",
    "сбан ", "сразбан ",
    "назначить ", "снять ", "разжаловать ", "повысить ", "понизить ",
    "удалить ", "закрепить ", "открепить ",
    "брак ", "развод ", "развестись ",
)

_COMMAND_WORDS = {
    "пред", "варн", "мут", "бан", "размут", "разбан",
    "сбан", "сразбан",
    "назначить", "снять", "разжаловать", "повысить", "понизить",
    "удалить", "закрепить", "открепить",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _command_form(text: str) -> str:
    return _normalize(text).rstrip(" ?？!！.")


def looks_like_mimorus_command(message: Message) -> bool:
    text = message.text or message.caption
    if not text:
        return False
    normalized = _normalize(text)
    if not normalized:
        return False
    if _START_CONNECT_RE.fullmatch(text.strip()):
        return False
    if normalized.startswith("/"):
        return True

    command = _command_form(text)
    if command in _EXACT_COMMANDS or command in _COMMAND_WORDS:
        return True
    return command.startswith(_COMMAND_PREFIXES)


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


def _content_list_count(config: dict | None, kind: str) -> int:
    key = "blocked_words" if kind == "words" else "blocked_phrases"
    raw = dict((config or {}).get(key) or {})
    lists = raw.get("lists")
    if isinstance(lists, list):
        return sum(1 for row in lists if isinstance(row, dict))
    # Legacy single-list config counts as one only when it actually contains a
    # configured list. This mirrors the content-filter router's migration view.
    if raw.get("items") or raw.get("enabled") or raw.get("action") or raw.get("mute_duration"):
        return 1
    return 0


class SubscriptionCommandNoticeMiddleware(BaseMiddleware):
    """Explain subscription expiry for group commands and stale private FSM flows."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _guard_private_fsm(self, event: Message, data: dict[str, Any]) -> bool:
        if event.chat.type != "private" or event.from_user is None:
            return False
        state = data.get("state")
        if not isinstance(state, FSMContext):
            return False
        state_name = await state.get_state()
        if state_name is None:
            return False
        state_data = await state.get_data()
        chat_id = _negative_chat_id(state_data)
        owner_wide = state_name in OWNER_WIDE_SUBSCRIPTION_STATES
        if chat_id is None and not owner_wide:
            return False

        try:
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

                # Creating a content-filter list is a two-step callback -> FSM
                # flow. Re-check the *current* tariff at commit time so a downgrade
                # between those steps cannot create an object above the new limit.
                content_limit: int | None = None
                content_count = 0
                if (
                    owner
                    and subscription is not None
                    and chat_id is not None
                    and state_name.endswith("ContentFilterState:waiting_list_name")
                ):
                    kind = str(state_data.get("cf_kind") or "")
                    if kind in {"words", "phrases"}:
                        limit_key = "blocked_word_lists" if kind == "words" else "blocked_phrase_lists"
                        content_limit = await effective_limit_for_owner(
                            session,
                            event.from_user.id,
                            limit_key,
                        )
                        config = (
                            await session.execute(
                                select(GroupSettings.moderation_config).where(
                                    GroupSettings.chat_id == chat_id
                                )
                            )
                        ).scalar_one_or_none() or {}
                        content_count = _content_list_count(config, kind)
        except Exception:
            return False

        if chat_id is not None and not owner:
            await state.clear()
            await event.answer(
                "⚠️ Эта настройка больше недоступна: вы не являетесь владельцем выбранной группы. "
                "Откройте нужную группу заново."
            )
            return True
        if subscription is None:
            await state.clear()
            await event.answer(EXPIRED_PRIVATE_FSM_TEXT)
            return True
        if content_limit is not None and content_count >= content_limit:
            await state.clear()
            await event.answer(
                f"⚠️ Текущий тариф разрешает до {content_limit} таких списков. "
                "Текущая настройка отменена и не была сохранена. Откройте раздел заново."
            )
            return True
        return False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and await self._guard_private_fsm(event, data):
            return None

        if (
            not isinstance(event, Message)
            or event.chat.type not in {"group", "supergroup"}
            or not looks_like_mimorus_command(event)
        ):
            return await handler(event, data)

        try:
            async with self.session_factory() as session:
                status = (
                    await session.execute(
                        select(Group.status).where(Group.chat_id == event.chat.id)
                    )
                ).scalar_one_or_none()
                if status != GroupStatus.active.value:
                    return await handler(event, data)
                subscription = await active_subscription_for_group(session, event.chat.id)
        except Exception:
            return await handler(event, data)

        if subscription is not None:
            return await handler(event, data)

        await event.reply(EXPIRED_SUBSCRIPTION_TEXT, parse_mode="HTML")
        return None
