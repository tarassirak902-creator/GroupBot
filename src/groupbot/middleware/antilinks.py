from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupSettings
from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import _execute_action, _group_ready
from groupbot.services.protected_members import is_protected_member

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"(?i)(?:(?:https?://)|(?:www\.))[^^\s<>]+")
DOMAIN_RE = re.compile(r"(?i)(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?:/[^\s<>]*)?")


def _host(raw: str) -> str | None:
    value = raw.strip().rstrip(".,!?;:)]}")
    if not value:
        return None
    if not re.match(r"(?i)^https?://", value):
        value = "https://" + value.removeprefix("www.")
    try:
        host = (urlparse(value).hostname or "").casefold().strip(".")
    except ValueError:
        return None
    return host.removeprefix("www.") or None


def _hosts(text: str) -> set[str]:
    found: set[str] = set()
    for match in URL_RE.findall(text):
        host = _host(match)
        if host:
            found.add(host)
    for match in DOMAIN_RE.findall(text):
        host = _host(match)
        if host:
            found.add(host)
    return found


def _allowed(host: str, whitelist: set[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in whitelist)


class AntiLinksMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)
        if event.from_user is None or event.from_user.is_bot:
            return await handler(event, data)
        text = event.text or event.caption or ""
        hosts = _hosts(text)
        if not hosts:
            return await handler(event, data)
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        action: str | None = None
        mute_duration: str | None = None
        async with self.session_factory() as session:
            if not await _group_ready(session, event.chat.id):
                return await handler(event, data)
            raw = (await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id == event.chat.id))).scalar_one_or_none() or {}
            cfg = dict(raw.get("antilinks") or {})
            if not cfg.get("enabled"):
                return await handler(event, data)
            action = str(cfg.get("action") or "warning")
            mute_duration = str(cfg.get("mute_duration") or "") or None
            if action not in {"warning", "mute"} or (action == "mute" and not mute_duration):
                return await handler(event, data)
            if await is_protected_member(session, chat_id=event.chat.id, user_id=event.from_user.id, moderation_config=raw):
                return await handler(event, data)

            whitelist = {str(x).casefold().removeprefix("www.").strip(".") for x in (raw.get("link_whitelist") or []) if str(x).strip()}
            blocked = {host for host in hosts if not _allowed(host, whitelist)}
            if not blocked:
                return await handler(event, data)

        try:
            await bot.delete_message(event.chat.id, event.message_id)
        except Exception:
            logger.info("Anti-links could not delete chat_id=%s message_id=%s", event.chat.id, event.message_id)

        try:
            bot_user = await bot.me()
            action_text = await _execute_action(bot=bot, session_factory=self.session_factory, chat_id=event.chat.id, actor=bot_user, target=event.from_user, action=action, reason="Запрещённая ссылка", duration_token=mute_duration)
            async with self.session_factory() as session:
                async with session.begin():
                    latest_id = (await session.execute(select(ModerationAction.id).where(ModerationAction.chat_id == event.chat.id, ModerationAction.target_user_id == event.from_user.id, ModerationAction.actor_user_id == bot_user.id, ModerationAction.action == action).order_by(ModerationAction.id.desc()).limit(1))).scalar_one_or_none()
                    if latest_id is not None:
                        await session.execute(update(ModerationAction).where(ModerationAction.id == latest_id).values(source="antilinks"))
            deleted_line = "🔗 Сообщение со ссылкой удалено."
            if action == "warning" and "\n\nБудьте аккуратнее!" in action_text:
                result_text = action_text.replace("\n\nБудьте аккуратнее!", f"\n\n{deleted_line}\n\nБудьте аккуратнее!", 1)
            else:
                result_text = f"{action_text}\n\n{deleted_line}"
            await bot.send_message(event.chat.id, result_text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("Anti-links action failed for chat_id=%s user_id=%s", event.chat.id, event.from_user.id)
        return await handler(event, data)
