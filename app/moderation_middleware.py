import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import FilterItem, FilterSet, GroupSettings, ModerationAction


def _normalize(text: str, case_sensitive: bool) -> str:
    value = unicodedata.normalize("NFKC", text)
    return value if case_sensitive else value.casefold()


def _matches(text: str, value: str, match_type: str, case_sensitive: bool) -> bool:
    haystack = _normalize(text, case_sensitive)
    needle = _normalize(value, case_sensitive)
    if match_type == "whole":
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack, flags=re.UNICODE) is not None
    return needle in haystack


class ModerationMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)
        if event.from_user is None or event.from_user.is_bot:
            return await handler(event, data)
        text = event.text or event.caption
        if not text or text.startswith("/"):
            return await handler(event, data)

        bot: Bot = data["bot"]
        async with self.session_factory() as session:
            settings_result = await session.execute(select(GroupSettings).where(GroupSettings.chat_id == event.chat.id))
            settings = settings_result.scalar_one_or_none()
            if settings is None or not settings.moderation_enabled:
                return await handler(event, data)

            result = await session.execute(
                select(FilterSet, FilterItem)
                .join(FilterItem, FilterItem.filter_set_id == FilterSet.id)
                .where(FilterSet.chat_id == event.chat.id, FilterSet.is_active.is_(True))
                .order_by(FilterSet.priority.desc(), FilterSet.id)
            )
            matches: list[tuple[FilterSet, FilterItem]] = []
            for filter_set, item in result.all():
                if _matches(text, item.value, filter_set.match_type, filter_set.case_sensitive):
                    matches.append((filter_set, item))

            if not matches:
                return await handler(event, data)

            if any(fs.exclude_admins for fs, _ in matches):
                member = await bot.get_chat_member(event.chat.id, event.from_user.id)
                if member.status in {"creator", "administrator"}:
                    matches = [(fs, it) for fs, it in matches if not fs.exclude_admins]
                    if not matches:
                        return await handler(event, data)

            effective_set, _ = matches[0]
            telegram_ok: bool | None = None
            telegram_error: str | None = None
            if effective_set.delete_message or effective_set.action == "delete":
                try:
                    await event.delete()
                    telegram_ok = True
                except Exception as exc:
                    telegram_ok = False
                    telegram_error = str(exc)[:500]

            for filter_set, item in matches:
                session.add(
                    ModerationAction(
                        chat_id=event.chat.id,
                        user_id=event.from_user.id,
                        message_id=event.message_id,
                        filter_set_id=filter_set.id,
                        filter_item_id=item.id,
                        matched_value=item.value,
                        action=effective_set.action,
                        reason=filter_set.reason,
                        telegram_ok=telegram_ok,
                        telegram_error=telegram_error,
                    )
                )
            await session.commit()

        if telegram_ok:
            return None
        return await handler(event, data)
