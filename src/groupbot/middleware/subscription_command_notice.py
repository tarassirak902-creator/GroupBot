from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group


EXPIRED_SUBSCRIPTION_TEXT = (
    "⚠️ <b>Mimorus не может выполнить это действие.</b>\n\n"
    "У владельца группы закончилась активная подписка. "
    "Команды и функции Mimorus в этой группе временно недоступны.\n\n"
    "Владельцу нужно продлить или активировать тариф в личных сообщениях с ботом."
)

_START_CONNECT_RE = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?\s+connect\s*$",
    re.IGNORECASE,
)

# Commands without arguments. Keep this catalog deliberately broader than one
# router so an expired subscription never turns a valid Mimorus command into
# silence merely because its handler lives in another module.
_EXACT_COMMANDS = {
    "кто я", "кто я?", "кто он", "кто он?", "кто она", "кто она?", "кто ты", "кто ты?",
    "топ админов", "топ администрации",
    "инфо", "информация", "профиль", "статистика", "стата",
    "нарушение",
    "банлист", "мутлист", "преды", "мои баны", "мои муты", "выдал пред", "сбанлист",
    "админы", "администрация",
    "очистить пользователя",
    "закрепи", "открепи",
    # Social/user commands are commands too: when the tariff is unavailable the
    # user must receive the same explanation instead of silence.
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
    if normalized in _EXACT_COMMANDS or normalized in _COMMAND_WORDS:
        return True
    return normalized.startswith(_COMMAND_PREFIXES)


class SubscriptionCommandNoticeMiddleware(BaseMiddleware):
    """Explain why Mimorus group commands are unavailable after subscription expiry.

    Ordinary conversation is never blocked. Only command-like group messages are
    intercepted, while /start connect remains available so a group can be connected
    before a tariff is activated.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
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
            # A temporary DB error must not swallow commands; let the normal router
            # produce its own error handling instead.
            return await handler(event, data)

        if subscription is not None:
            return await handler(event, data)

        await event.reply(EXPIRED_SUBSCRIPTION_TEXT, parse_mode="HTML")
        return None
