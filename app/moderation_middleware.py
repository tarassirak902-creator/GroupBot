import re
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import ChatPermissions, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    FilterItem,
    FilterSet,
    GroupSettings,
    ModerationAction,
    ModerationWarning,
    ModerationWhitelist,
)

ACTION_WEIGHT = {"delete": 1, "warning": 2, "mute": 3, "ban": 4}


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
            settings = (await session.execute(
                select(GroupSettings).where(GroupSettings.chat_id == event.chat.id)
            )).scalar_one_or_none()
            if settings is None or not settings.moderation_enabled:
                return await handler(event, data)

            result = await session.execute(
                select(FilterSet, FilterItem)
                .join(FilterItem, FilterItem.filter_set_id == FilterSet.id)
                .where(FilterSet.chat_id == event.chat.id, FilterSet.is_active.is_(True))
            )
            matches = [
                (filter_set, item)
                for filter_set, item in result.all()
                if _matches(text, item.value, filter_set.match_type, filter_set.case_sensitive)
            ]
            if not matches:
                return await handler(event, data)

            member = None
            if any(fs.exclude_admins for fs, _ in matches):
                try:
                    member = await bot.get_chat_member(event.chat.id, event.from_user.id)
                except Exception:
                    member = None
                if member is not None and member.status in {"creator", "administrator"}:
                    matches = [(fs, it) for fs, it in matches if not fs.exclude_admins]

            if matches and any(fs.exclude_whitelist for fs, _ in matches):
                whitelisted = (await session.execute(
                    select(ModerationWhitelist.id).where(
                        ModerationWhitelist.chat_id == event.chat.id,
                        ModerationWhitelist.user_id == event.from_user.id,
                    )
                )).scalar_one_or_none() is not None
                if whitelisted:
                    matches = [(fs, it) for fs, it in matches if not fs.exclude_whitelist]

            if not matches:
                return await handler(event, data)

            effective_set, _ = max(
                matches,
                key=lambda pair: (ACTION_WEIGHT.get(pair[0].action, 0), pair[0].priority),
            )
            should_delete = any(fs.delete_message or fs.action == "delete" for fs, _ in matches)
            telegram_ok: bool | None = True
            errors: list[str] = []

            if should_delete:
                try:
                    await event.delete()
                except Exception as exc:
                    telegram_ok = False
                    errors.append(f"delete: {exc}")

            if effective_set.action == "warning":
                session.add(ModerationWarning(
                    chat_id=event.chat.id,
                    user_id=event.from_user.id,
                    filter_set_id=effective_set.id,
                    reason=effective_set.reason,
                ))
                try:
                    await bot.send_message(
                        event.chat.id,
                        f"⚠️ {event.from_user.full_name}: предупреждение."
                        + (f" Причина: {effective_set.reason}" if effective_set.reason else ""),
                    )
                except Exception as exc:
                    telegram_ok = False
                    errors.append(f"warning_notice: {exc}")

            elif effective_set.action == "mute":
                if not effective_set.mute_seconds or effective_set.mute_seconds <= 0:
                    telegram_ok = False
                    errors.append("mute: duration is not configured")
                else:
                    try:
                        await bot.restrict_chat_member(
                            chat_id=event.chat.id,
                            user_id=event.from_user.id,
                            permissions=ChatPermissions(can_send_messages=False),
                            until_date=datetime.now(timezone.utc) + timedelta(seconds=effective_set.mute_seconds),
                        )
                    except Exception as exc:
                        telegram_ok = False
                        errors.append(f"mute: {exc}")

            elif effective_set.action == "ban":
                try:
                    await bot.ban_chat_member(event.chat.id, event.from_user.id)
                except Exception as exc:
                    telegram_ok = False
                    errors.append(f"ban: {exc}")

            telegram_error = "; ".join(errors)[:500] if errors else None
            for filter_set, item in matches:
                session.add(ModerationAction(
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
                ))
            await session.commit()

        if should_delete or effective_set.action in {"mute", "ban"}:
            return None
        return await handler(event, data)
