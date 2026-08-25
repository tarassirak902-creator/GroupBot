from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.models import GroupSettings
from groupbot.services.protected_members import is_protected_member

logger = logging.getLogger(__name__)


class AdvertisingMandatoryMiddleware(BaseMiddleware):
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
            placements = list((await session.execute(
                select(AdvertisingPlacement)
                .join(AdvertisingDeal, AdvertisingDeal.id == AdvertisingPlacement.deal_id)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(
                    AdvertisingListing.chat_id == event.chat.id,
                    AdvertisingPlacement.kind == "mandatory",
                    AdvertisingPlacement.status == "active",
                    AdvertisingDeal.status == "accepted",
                )
                .order_by(AdvertisingPlacement.starts_at.asc(), AdvertisingPlacement.id.asc())
            )).scalars().all())
            if not placements:
                return await handler(event, data)

            moderation_config = (await session.execute(
                select(GroupSettings.moderation_config).where(GroupSettings.chat_id == event.chat.id)
            )).scalar_one_or_none() or {}
            if await is_protected_member(
                session,
                chat_id=event.chat.id,
                user_id=event.from_user.id,
                moderation_config=moderation_config,
            ):
                return await handler(event, data)

        missing: dict | None = None
        for placement in placements:
            cfg = dict(placement.config_json or {})
            target_chat_id = cfg.get("target_chat_id")
            target_url = str(cfg.get("target_url") or "")
            target_username = str(cfg.get("target_username") or "")
            if not isinstance(target_chat_id, int) or not target_url:
                continue
            try:
                member = await bot.get_chat_member(target_chat_id, event.from_user.id)
            except Exception:
                # Do not lock the advertiser's group if Telegram temporarily cannot
                # verify the sponsor chat or the bot lost access there.
                logger.exception(
                    "Could not verify advertising OP membership target_chat_id=%s user_id=%s",
                    target_chat_id,
                    event.from_user.id,
                )
                continue
            if member.status not in {"member", "administrator", "creator", "restricted"}:
                missing = {
                    "url": target_url,
                    "username": target_username,
                }
                break
            if member.status == "restricted" and not getattr(member, "is_member", True):
                missing = {
                    "url": target_url,
                    "username": target_username,
                }
                break

        if missing is None:
            return await handler(event, data)

        try:
            await bot.delete_message(event.chat.id, event.message_id)
        except Exception:
            logger.info(
                "Could not delete message blocked by advertising OP chat_id=%s message_id=%s",
                event.chat.id,
                event.message_id,
            )

        username = f"@{event.from_user.username}" if event.from_user.username else event.from_user.full_name
        sponsor = f"@{missing['username']}" if missing.get("username") else missing["url"]
        text = (
            f"{escape(username)}, чтобы писать в группе, Вам необходимо подписаться на :\n"
            f"{escape(sponsor)}"
        )
        try:
            await bot.send_message(
                event.chat.id,
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Подписаться", url=missing["url"])]
                ]),
            )
        except Exception:
            logger.exception("Could not send advertising OP notice chat_id=%s", event.chat.id)
        return None
