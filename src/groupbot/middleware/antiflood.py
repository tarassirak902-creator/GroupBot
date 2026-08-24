from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, GroupSettings
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.manual_moderation import _execute_action, _group_ready
from groupbot.services.permissions import is_group_owner

logger = logging.getLogger(__name__)


class AntiFloodMiddleware(BaseMiddleware):
    """Apply configured per-group anti-flood before ordinary message routers."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _is_admin(self, session: AsyncSession, chat_id: int, user_id: int) -> bool:
        if await is_group_owner(session, chat_id, user_id):
            return True
        assignment = (await session.execute(select(AdminAssignment.id).where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == user_id))).scalar_one_or_none()
        return assignment is not None

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)
        if event.from_user is None or event.from_user.is_bot:
            return await handler(event, data)
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        now = datetime.now(timezone.utc)
        action: str | None = None
        mute_duration: str | None = None
        should_trigger = False

        async with self.session_factory() as session:
            if not await _group_ready(session, event.chat.id):
                return await handler(event, data)
            raw = (await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id == event.chat.id))).scalar_one_or_none() or {}
            cfg = dict(raw.get("antiflood") or {})
            if not cfg.get("enabled"):
                return await handler(event, data)
            try:
                message_limit = int(cfg.get("message_limit")); window_seconds = int(cfg.get("window_seconds"))
            except (TypeError, ValueError):
                return await handler(event, data)
            action = str(cfg.get("action") or ""); mute_duration = str(cfg.get("mute_duration") or "") or None
            if message_limit < 2 or window_seconds <= 0 or action not in {"warning", "mute"}:
                return await handler(event, data)
            if action == "mute" and not mute_duration:
                return await handler(event, data)

            # Privileged roles are always immune; this is a bot invariant, not a user setting.
            if await self._is_admin(session, event.chat.id, event.from_user.id):
                return await handler(event, data)
            special = dict(raw.get("special_statuses") or {})
            protected_ids = {int(x) for x in (special.get("vip") or [])} | {int(x) for x in (special.get("nedotroga") or [])}
            if event.from_user.id in protected_ids:
                return await handler(event, data)

            cutoff = now - timedelta(seconds=window_seconds)
            count = (await session.execute(select(func.count()).select_from(ObservedMessage).where(ObservedMessage.chat_id == event.chat.id, ObservedMessage.user_id == event.from_user.id, ObservedMessage.sent_at >= cutoff))).scalar_one()
            if count < message_limit:
                return await handler(event, data)
            recent = (await session.execute(select(ModerationAction.id).where(ModerationAction.chat_id == event.chat.id, ModerationAction.target_user_id == event.from_user.id, ModerationAction.action == action, ModerationAction.source == "antiflood", ModerationAction.created_at >= cutoff).order_by(ModerationAction.id.desc()).limit(1))).scalar_one_or_none()
            should_trigger = recent is None

        if should_trigger and action is not None:
            try:
                bot_user = await bot.me()
                text = await _execute_action(bot=bot, session_factory=self.session_factory, chat_id=event.chat.id, actor=bot_user, target=event.from_user, action=action, reason="Антифлуд", duration_token=mute_duration)
                async with self.session_factory() as session:
                    async with session.begin():
                        latest_id = (await session.execute(select(ModerationAction.id).where(ModerationAction.chat_id == event.chat.id, ModerationAction.target_user_id == event.from_user.id, ModerationAction.actor_user_id == bot_user.id, ModerationAction.action == action).order_by(ModerationAction.id.desc()).limit(1))).scalar_one_or_none()
                        if latest_id is not None:
                            await session.execute(update(ModerationAction).where(ModerationAction.id == latest_id).values(source="antiflood"))
                await bot.send_message(event.chat.id, text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                logger.exception("Anti-flood action failed for chat_id=%s user_id=%s", event.chat.id, event.from_user.id)
        return await handler(event, data)
