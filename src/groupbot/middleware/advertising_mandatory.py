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
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.models import AdminAssignment, GroupOwner, GroupSettings

logger = logging.getLogger(__name__)


async def _is_op_exempt_in_db(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    owner = (await session.execute(select(GroupOwner.id).where(
        GroupOwner.chat_id == chat_id,
        GroupOwner.user_id == user_id,
        GroupOwner.is_current.is_(True),
    ).limit(1))).scalar_one_or_none()
    if owner is not None:
        return True
    admin = (await session.execute(select(AdminAssignment.id).where(
        AdminAssignment.chat_id == chat_id,
        AdminAssignment.user_id == user_id,
    ).limit(1))).scalar_one_or_none()
    if admin is not None:
        return True
    moderation_config = (await session.execute(select(GroupSettings.moderation_config).where(
        GroupSettings.chat_id == chat_id
    ))).scalar_one_or_none() or {}
    special = dict(moderation_config.get("special_statuses") or {})
    vip_ids = {
        int(value) for value in (special.get("vip") or [])
        if str(value).lstrip("-").isdigit()
    }
    return user_id in vip_ids


class AdvertisingMandatoryMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        # All advertising modules are imported before middleware instances are created
        # in main.py, so this is a safe point to replace legacy deal labels/buttons.
        from groupbot.routers.advertising_mutual_patches import install_mutual_ui_patches
        install_mutual_ui_patches()

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)
        if event.from_user is None or event.from_user.is_bot:
            return await handler(event, data)
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        requirements: list[dict[str, Any]] = []
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
            for placement in placements:
                cfg = dict(placement.config_json or {})
                target_chat_id = cfg.get("target_chat_id")
                target_url = str(cfg.get("target_url") or "")
                if isinstance(target_chat_id, int) and target_url:
                    requirements.append({
                        "target_chat_id": target_chat_id,
                        "url": target_url,
                        "title": str(cfg.get("target_title") or cfg.get("target_username") or "Группа"),
                    })

            mutual = list((await session.execute(
                select(AdvertisingMutualOpDirection).where(
                    AdvertisingMutualOpDirection.source_chat_id == event.chat.id,
                    AdvertisingMutualOpDirection.status == "active",
                ).order_by(AdvertisingMutualOpDirection.starts_at, AdvertisingMutualOpDirection.id)
            )).scalars().all())
            for direction in mutual:
                if direction.invite_link:
                    requirements.append({
                        "target_chat_id": direction.target_chat_id,
                        "url": direction.invite_link,
                        "title": direction.target_title,
                    })

            if not requirements:
                return await handler(event, data)
            if await _is_op_exempt_in_db(session, event.chat.id, event.from_user.id):
                return await handler(event, data)

        try:
            member_in_advertiser = await bot.get_chat_member(event.chat.id, event.from_user.id)
            if member_in_advertiser.status in {"administrator", "creator"}:
                return await handler(event, data)
        except Exception:
            logger.info("Could not verify Telegram admin exemption chat_id=%s user_id=%s", event.chat.id, event.from_user.id)

        missing: dict[str, Any] | None = None
        for requirement in requirements:
            try:
                member = await bot.get_chat_member(requirement["target_chat_id"], event.from_user.id)
            except Exception:
                logger.exception("Could not verify advertising OP membership target_chat_id=%s user_id=%s", requirement["target_chat_id"], event.from_user.id)
                continue
            joined = member.status in {"member", "administrator", "creator"} or (
                member.status == "restricted" and getattr(member, "is_member", True)
            )
            if not joined:
                missing = requirement
                break

        if missing is None:
            return await handler(event, data)

        try:
            await bot.delete_message(event.chat.id, event.message_id)
        except Exception:
            logger.info("Could not delete message blocked by advertising OP chat_id=%s message_id=%s", event.chat.id, event.message_id)

        user_name = event.from_user.full_name or event.from_user.username or "Пользователь"
        user_link = f'<a href="tg://user?id={event.from_user.id}">{escape(user_name)}</a>'
        text = f"👤 {user_link}, чтобы писать в группе, Вам необходимо подписаться на:"
        button_title = str(missing["title"]).strip() or "Группа"
        if len(button_title) > 48:
            button_title = button_title[:47].rstrip() + "…"
        try:
            await bot.send_message(
                event.chat.id,
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=f"🏠 {button_title}", url=missing["url"])
                ]]),
            )
        except Exception:
            logger.exception("Could not send advertising OP notice chat_id=%s", event.chat.id)
        return None
