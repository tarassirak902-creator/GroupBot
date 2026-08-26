from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.middleware.advertising_mandatory import _is_op_exempt_in_db

logger = logging.getLogger(__name__)


class AdvertisingMutualOpMiddleware(BaseMiddleware):
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

        async with self.session_factory() as session:
            directions = list((await session.execute(
                select(AdvertisingMutualOpDirection).where(
                    AdvertisingMutualOpDirection.source_chat_id == event.chat.id,
                    AdvertisingMutualOpDirection.status == "active",
                ).order_by(AdvertisingMutualOpDirection.starts_at, AdvertisingMutualOpDirection.id)
            )).scalars().all())
            if not directions:
                return await handler(event, data)
            if await _is_op_exempt_in_db(session, event.chat.id, event.from_user.id):
                return await handler(event, data)

        try:
            source_member = await bot.get_chat_member(event.chat.id, event.from_user.id)
            if source_member.status in {"administrator", "creator"}:
                return await handler(event, data)
        except Exception:
            pass

        missing: AdvertisingMutualOpDirection | None = None
        for direction in directions:
            try:
                member = await bot.get_chat_member(direction.target_chat_id, event.from_user.id)
            except Exception:
                logger.exception("Could not verify mutual OP membership direction=%s user=%s", direction.id, event.from_user.id)
                continue
            joined = member.status in {"member", "administrator", "creator"} or (
                member.status == "restricted" and getattr(member, "is_member", True)
            )
            if not joined:
                missing = direction
                break

        if missing is None:
            return await handler(event, data)

        try:
            await bot.delete_message(event.chat.id, event.message_id)
        except Exception:
            logger.info("Could not delete message blocked by mutual OP chat=%s message=%s", event.chat.id, event.message_id)

        user_name = event.from_user.full_name or event.from_user.username or "Пользователь"
        user_link = f'<a href="tg://user?id={event.from_user.id}">{escape(user_name)}</a>'
        title = missing.target_title.strip() or "Группа"
        if len(title) > 48:
            title = title[:47].rstrip() + "…"
        try:
            await bot.send_message(
                event.chat.id,
                f"👤 {user_link}, чтобы писать в группе, Вам необходимо подписаться на:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=f"🏠 {title}", url=missing.invite_link)
                ]]),
            )
        except Exception:
            logger.exception("Could not send mutual OP notice chat=%s", event.chat.id)
        return None
