from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, GroupSettings
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.manual_moderation import _execute_action, _group_ready
from groupbot.routers.user_display import clickable_identity
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

        action: str | None = None
        mute_duration: str | None = None
        repeated_message_ids: list[int] = []

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
            if repeat_count < 2 or window_seconds <= 0 or not 1 <= similarity_percent <= 100 or action not in {"warning", "mute"}:
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
            candidates = list((await session.execute(
                select(
                    ObservedMessage.message_id,
                    ObservedMessage.normalized_text,
                    ObservedMessage.sent_at,
                    ObservedMessage.deleted_at,
                ).where(
                    ObservedMessage.chat_id == event.chat.id,
                    ObservedMessage.user_id == event.from_user.id,
                    ObservedMessage.sent_at >= cutoff,
                    ObservedMessage.normalized_text.is_not(None),
                ).order_by(ObservedMessage.sent_at.asc(), ObservedMessage.message_id.asc()).limit(500)
            )).all())

            threshold = similarity_percent / 100.0
            similar_rows = [
                row for row in candidates
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
                    await session.execute(
                        update(ObservedMessage)
                        .where(
                            ObservedMessage.chat_id == event.chat.id,
                            ObservedMessage.message_id.in_(deleted_ids),
                        )
                        .values(deleted_at=datetime.now(timezone.utc))
                    )

        if action is not None:
            try:
                bot_user = await bot.me()
                await _execute_action(
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
                            await session.execute(
                                update(ModerationAction)
                                .where(ModerationAction.id == latest_id)
                                .values(source="antispam")
                            )
                        warning_count = int((await session.execute(
                            select(func.count()).select_from(ModerationAction).where(
                                ModerationAction.chat_id == event.chat.id,
                                ModerationAction.target_user_id == event.from_user.id,
                                ModerationAction.action == "warning",
                                ModerationAction.is_active.is_(True),
                            )
                        )).scalar_one())

                target_text = clickable_identity(
                    telegram_user_id=event.from_user.id,
                    first_name=event.from_user.first_name,
                    last_name=event.from_user.last_name,
                    username=event.from_user.username,
                )
                if action == "warning":
                    shown_count = min(warning_count, 5)
                    if shown_count >= 5:
                        header = "⛔ Антиспам"
                        marker = " ⛔"
                        punishment = "бан"
                    elif shown_count == 4:
                        header = "⚠️ Антиспам"
                        marker = " 🔇"
                        punishment = "мут на 1 час"
                    elif shown_count == 3:
                        header = "⚠️ Антиспам"
                        marker = " 🔇"
                        punishment = "мут на 15 минут"
                    else:
                        header = "⚠️ Антиспам"
                        marker = ""
                        punishment = "предупреждение"
                    text = (
                        f"<b>{header}</b>\n\n"
                        f"👤 {target_text}\n"
                        "🧹 Повторное сообщение удалено.\n\n"
                        f"⚠️ Предупреждения: <b>{shown_count}/5</b>{marker}\n"
                        f"Наказание: <b>{punishment}</b> 📌\n"
                        "Причина: <b>Антиспам</b>"
                    )
                else:
                    text = (
                        "<b>🔇 Антиспам</b>\n\n"
                        f"👤 {target_text}\n"
                        "🧹 Повторное сообщение удалено.\n\n"
                        f"Наказание: <b>мут {escape(mute_duration or '')}</b> 📌\n"
                        "Причина: <b>Антиспам</b>"
                    )
                await bot.send_message(
                    event.chat.id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Anti-spam action failed for chat_id=%s user_id=%s", event.chat.id, event.from_user.id)

        return await handler(event, data)
