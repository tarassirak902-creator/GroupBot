from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupSettings
from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import _execute_action, _group_ready
from groupbot.services.protected_members import is_protected_member

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _word_hit(text: str, items: list[str]) -> str | None:
    for item in items:
        token = item.strip().casefold()
        if not token:
            continue
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, flags=re.UNICODE):
            return item
    return None


def _phrase_hit(text: str, items: list[str]) -> str | None:
    for item in items:
        token = _normalize(item)
        if token and token in text:
            return item
    return None


class ContentFiltersMiddleware(BaseMiddleware):
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
        raw_text = event.text or event.caption or ""
        if not raw_text.strip():
            return await handler(event, data)
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        normalized = _normalize(raw_text)
        action: str | None = None
        mute_duration: str | None = None
        reason: str | None = None
        source: str | None = None

        async with self.session_factory() as session:
            if not await _group_ready(session, event.chat.id):
                return await handler(event, data)
            root = (
                await session.execute(
                    select(GroupSettings.moderation_config).where(GroupSettings.chat_id == event.chat.id)
                )
            ).scalar_one_or_none() or {}
            if await is_protected_member(
                session,
                chat_id=event.chat.id,
                user_id=event.from_user.id,
                moderation_config=root,
            ):
                return await handler(event, data)

            checks = (
                ("blocked_words", "Запрещённое слово", "blocked_words", _word_hit),
                ("blocked_phrases", "Запрещённая фраза", "blocked_phrases", _phrase_hit),
            )
            for key, reason_label, source_label, matcher in checks:
                cfg = dict(root.get(key) or {})
                if not cfg.get("enabled"):
                    continue
                items = [str(x) for x in (cfg.get("items") or []) if str(x).strip()]
                if matcher(normalized, items) is None:
                    continue
                candidate_action = str(cfg.get("action") or "warning")
                candidate_duration = str(cfg.get("mute_duration") or "") or None
                if candidate_action not in {"warning", "mute"}:
                    continue
                if candidate_action == "mute" and not candidate_duration:
                    continue
                action = candidate_action
                mute_duration = candidate_duration
                reason = reason_label
                source = source_label
                break

        if action is None or reason is None or source is None:
            return await handler(event, data)

        try:
            await bot.delete_message(event.chat.id, event.message_id)
        except Exception:
            logger.info("Content filter could not delete chat_id=%s message_id=%s", event.chat.id, event.message_id)

        try:
            bot_user = await bot.me()
            action_text = await _execute_action(
                bot=bot,
                session_factory=self.session_factory,
                chat_id=event.chat.id,
                actor=bot_user,
                target=event.from_user,
                action=action,
                reason=reason,
                duration_token=mute_duration,
            )
            async with self.session_factory() as session:
                async with session.begin():
                    latest_id = (
                        await session.execute(
                            select(ModerationAction.id)
                            .where(
                                ModerationAction.chat_id == event.chat.id,
                                ModerationAction.target_user_id == event.from_user.id,
                                ModerationAction.actor_user_id == bot_user.id,
                                ModerationAction.action == action,
                            )
                            .order_by(ModerationAction.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if latest_id is not None:
                        await session.execute(
                            update(ModerationAction)
                            .where(ModerationAction.id == latest_id)
                            .values(source=source)
                        )
            deleted_line = "🗑 Сообщение удалено."
            if action == "warning" and "\n\nБудьте аккуратнее!" in action_text:
                result = action_text.replace(
                    "\n\nБудьте аккуратнее!",
                    f"\n\n{deleted_line}\n\nБудьте аккуратнее!",
                    1,
                )
            else:
                result = f"{action_text}\n\n{deleted_line}"
            await bot.send_message(
                event.chat.id,
                result,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Content filter action failed for chat_id=%s user_id=%s", event.chat.id, event.from_user.id)

        return await handler(event, data)
