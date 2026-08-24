from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, GroupSettings
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.manual_moderation import _execute_action, _group_ready
from groupbot.services.permissions import is_group_owner

logger = logging.getLogger(__name__)


class AntiSpamMiddleware(BaseMiddleware):
    """Detect repeated or near-duplicate messages using persisted observed text."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _is_admin(self, session: AsyncSession, chat_id: int, user_id: int) -> bool:
        if await is_group_owner(session, chat_id, user_id):
            return True
        return (
            await session.execute(
                select(AdminAssignment.id).where(
                    AdminAssignment.chat_id == chat_id,
                    AdminAssignment.user_id == user_id,
                )
            )
        ).scalar_one_or_none() is not None

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

        current_text = None
        action = None
        mute_duration = None
        should_trigger = False
        cutoff = None

        async with self.session_factory() as session:
            if not await _group_ready(session, event.chat.id):
                return await handler(event, data)
            raw = (
                await session.execute(
                    select(GroupSettings.moderation_config).where(GroupSettings.chat_id == event.chat.id)
                )
            ).scalar_one_or_none() or {}
            cfg = dict(raw.get("antispam") or {})
            if not cfg.get("enabled"):
                return await handler(event, data)

            try:
                repeat_count = int(cfg.get("repeat_count"))
                window_seconds = int(cfg.get("window_seconds"))
                similarity_percent = int(cfg.get("similarity_percent"))
            except (TypeError, ValueError):
                return await handler(event, data)
            action = str(cfg.get("action") or "")
            mute_duration = str(cfg.get("mute_duration") or "") or None
            if repeat_count < 2 or window_seconds <= 0 or not 1 <= similarity_percent <= 100 or action not in {"warning", "mute", "ban"}:
                return await handler(event, data)
            if action == "mute" and not mute_duration:
                return await handler(event, data)

            exclusions = dict(cfg.get("exclusions") or {})
            if exclusions.get("admins") and await self._is_admin(session, event.chat.id, event.from_user.id):
                return await handler(event, data)
            special = dict(raw.get("special_statuses") or {})
            if exclusions.get("vip") and event.from_user.id in {int(x) for x in special.get("vip") or []}:
                return await handler(event, data)
            if exclusions.get("nedotroga") and event.from_user.id in {int(x) for x in special.get("nedotroga") or []}:
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
            previous = list((await session.execute(
                select(ObservedMessage.normalized_text).where(
                    ObservedMessage.chat_id == event.chat.id,
                    ObservedMessage.user_id == event.from_user.id,
                    ObservedMessage.sent_at >= cutoff,
                    ObservedMessage.message_id != event.message_id,
                    ObservedMessage.normalized_text.is_not(None),
                ).order_by(ObservedMessage.sent_at.desc()).limit(100)
            )).scalars().all())

            threshold = similarity_percent / 100.0
            similar_count = 1
            for candidate in previous:
                if not candidate:
                    continue
                if SequenceMatcher(None, current_text, candidate).ratio() >= threshold:
                    similar_count += 1
                    if similar_count >= repeat_count:
                        break
            if similar_count < repeat_count:
                return await handler(event, data)

            recent = (
                await session.execute(
                    select(ModerationAction.id).where(
                        ModerationAction.chat_id == event.chat.id,
                        ModerationAction.target_user_id == event.from_user.id,
                        ModerationAction.action == action,
                        ModerationAction.source == "antispam",
                        ModerationAction.created_at >= cutoff,
                    ).order_by(ModerationAction.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            should_trigger = recent is None

        if should_trigger and action is not None:
            try:
                bot_user = await bot.me()
                text = await _execute_action(
                    bot=bot,
                    session_factory=self.session_factory,
                    chat_id=event.chat.id,
                    actor=bot_user,
                    target=event.from_user,
                    action=action,
                    reason="Антиспам",
                    duration_token=mute_duration,
                )
                async with self.session_factory() as session:
                    async with session.begin():
                        latest_id = (
                            await session.execute(
                                select(ModerationAction.id).where(
                                    ModerationAction.chat_id == event.chat.id,
                                    ModerationAction.target_user_id == event.from_user.id,
                                    ModerationAction.actor_user_id == bot_user.id,
                                    ModerationAction.action == action,
                                ).order_by(ModerationAction.id.desc()).limit(1)
                            )
                        ).scalar_one_or_none()
                        if latest_id is not None:
                            await session.execute(update(ModerationAction).where(ModerationAction.id == latest_id).values(source="antispam"))
                await bot.send_message(event.chat.id, text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                logger.exception("Anti-spam action failed for chat_id=%s user_id=%s", event.chat.id, event.from_user.id)

        return await handler(event, data)
