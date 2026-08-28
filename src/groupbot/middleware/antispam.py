from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupSettings
from groupbot.moderation_models import ObservedMessage
from groupbot.routers.manual_moderation import _execute_action, _group_ready
from groupbot.services.automatic_moderation import (
    claim_automatic_moderation,
    mark_observed_deleted,
)
from groupbot.services.protected_members import is_protected_member
from groupbot.services.protection_schedule import protection_enabled

logger = logging.getLogger(__name__)


class AntiSpamMiddleware(BaseMiddleware):
    """Detect repeated or near-duplicate messages using persisted observed text."""

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
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        action: str | None = None
        mute_duration: str | None = None
        repeated_message_ids: list[int] = []

        async with self.session_factory() as session:
            if not await _group_ready(session, event.chat.id):
                return await handler(event, data)
            raw = (
                await session.execute(
                    select(GroupSettings.moderation_config).where(
                        GroupSettings.chat_id == event.chat.id
                    )
                )
            ).scalar_one_or_none() or {}
            cfg = dict(raw.get("antispam") or {})
            if not protection_enabled(raw, "antispam", bool(cfg.get("enabled"))):
                return await handler(event, data)
            try:
                repeat_count = int(cfg.get("repeat_count"))
                window_seconds = int(cfg.get("window_seconds"))
                similarity_percent = int(cfg.get("similarity_percent"))
            except (TypeError, ValueError):
                return await handler(event, data)
            action = str(cfg.get("action") or "")
            mute_duration = str(cfg.get("mute_duration") or "") or None
            if (
                repeat_count < 2
                or window_seconds <= 0
                or not 1 <= similarity_percent <= 100
                or action not in {"warning", "mute"}
            ):
                return await handler(event, data)
            if action == "mute" and not mute_duration:
                return await handler(event, data)
            if await is_protected_member(
                session,
                chat_id=event.chat.id,
                user_id=event.from_user.id,
                moderation_config=raw,
            ):
                return await handler(event, data)

            current_text = (
                await session.execute(
                    select(ObservedMessage.normalized_text).where(
                        ObservedMessage.chat_id == event.chat.id,
                        ObservedMessage.message_id == event.message_id,
                    )
                )
            ).scalar_one_or_none()
            if not current_text:
                return await handler(event, data)
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
            candidates = list((
                await session.execute(
                    select(
                        ObservedMessage.message_id,
                        ObservedMessage.normalized_text,
                        ObservedMessage.sent_at,
                        ObservedMessage.deleted_at,
                    )
                    .where(
                        ObservedMessage.chat_id == event.chat.id,
                        ObservedMessage.user_id == event.from_user.id,
                        ObservedMessage.sent_at >= cutoff,
                        ObservedMessage.normalized_text.is_not(None),
                    )
                    .order_by(ObservedMessage.sent_at.asc(), ObservedMessage.message_id.asc())
                    .limit(500)
                )
            ).all())
            threshold = similarity_percent / 100.0
            similar_rows = [
                row
                for row in candidates
                if row.normalized_text
                and SequenceMatcher(None, current_text, row.normalized_text).ratio() >= threshold
            ]
            if len(similar_rows) < repeat_count:
                return await handler(event, data)
            repeated_message_ids = [
                int(row.message_id)
                for row in similar_rows[1:]
                if row.deleted_at is None
            ]
            if event.message_id not in repeated_message_ids:
                return await handler(event, data)

        # Cleanup is independent from punishment ownership. Another protection
        # may already have claimed the update, but repeated spam should still be
        # removed from the chat and reflected in deletion statistics.
        deleted_ids: list[int] = []
        for message_id in repeated_message_ids:
            try:
                await bot.delete_message(event.chat.id, message_id)
                deleted_ids.append(message_id)
            except Exception:
                logger.info(
                    "Anti-spam could not delete chat_id=%s message_id=%s",
                    event.chat.id,
                    message_id,
                )
        if deleted_ids:
            async with self.session_factory() as session:
                async with session.begin():
                    await mark_observed_deleted(
                        session,
                        chat_id=event.chat.id,
                        message_ids=deleted_ids,
                        deleted_at=datetime.now(timezone.utc),
                    )

        if not claim_automatic_moderation(data, "antispam"):
            return await handler(event, data)

        if action is not None:
            try:
                bot_user = await bot.me()
                action_text = await _execute_action(
                    bot=bot,
                    session_factory=self.session_factory,
                    chat_id=event.chat.id,
                    actor=bot_user,
                    target=event.from_user,
                    action=action,
                    reason="Антиспам",
                    duration_token=mute_duration,
                    source="antispam",
                )
                deleted_line = "🧹 Повторное сообщение удалено."
                if action == "warning" and "\n\nБудьте аккуратнее!" in action_text:
                    text = action_text.replace(
                        "\n\nБудьте аккуратнее!",
                        f"\n\n{deleted_line}\n\nБудьте аккуратнее!",
                        1,
                    )
                else:
                    text = f"{action_text}\n\n{deleted_line}"
                await bot.send_message(
                    event.chat.id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception(
                    "Anti-spam action failed for chat_id=%s user_id=%s",
                    event.chat.id,
                    event.from_user.id,
                )
        return await handler(event, data)
