from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.services.audit import write_audit

logger = logging.getLogger(__name__)


def _post_keyboard(cfg: dict) -> InlineKeyboardMarkup | None:
    text = str(cfg.get("button_text") or "").strip()
    url = str(cfg.get("button_url") or "").strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text[:64], url=url)]])


async def publish_due_posts(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    published = 0
    async with session_factory() as session:
        async with session.begin():
            rows = (await session.execute(
                select(AdvertisingPlacement, AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingDeal, AdvertisingDeal.id == AdvertisingPlacement.deal_id)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(
                    AdvertisingPlacement.kind == "post",
                    AdvertisingPlacement.status == "active",
                    AdvertisingPlacement.starts_at <= now,
                    AdvertisingPlacement.ends_at > now,
                )
                .with_for_update(skip_locked=True)
            )).all()
            for placement, deal, listing in rows:
                cfg = dict(placement.config_json or {})
                interval = max(int(cfg.get("interval_minutes") or 60), 1)
                if placement.last_published_at is not None and placement.last_published_at > now - timedelta(minutes=interval):
                    continue
                try:
                    markup = _post_keyboard(cfg)
                    text = str(cfg.get("text") or "")
                    photo = cfg.get("photo_file_id")
                    if photo:
                        await bot.send_photo(listing.chat_id, photo=photo, caption=text or None, reply_markup=markup)
                    else:
                        await bot.send_message(listing.chat_id, text, reply_markup=markup)
                    published += 1
                    await write_audit(
                        session,
                        "advertising.post_published",
                        actor_user_id=None,
                        chat_id=listing.chat_id,
                        target_type="advertising_deal",
                        target_id=str(deal.id),
                        payload={"placement_id": placement.id},
                    )
                except Exception:
                    logger.exception("Failed to publish advertising post deal=%s chat=%s", deal.id, listing.chat_id)
                finally:
                    placement.last_published_at = now
    return published


async def finish_expired_posts(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    notifications: list[tuple[int, int, str, bool, int]] = []
    finished = 0
    async with session_factory() as session:
        async with session.begin():
            rows = (await session.execute(
                select(AdvertisingPlacement, AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingDeal, AdvertisingDeal.id == AdvertisingPlacement.deal_id)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(
                    AdvertisingPlacement.kind == "post",
                    AdvertisingPlacement.status == "active",
                    AdvertisingPlacement.ends_at <= now,
                )
                .with_for_update(skip_locked=True)
            )).all()
            for placement, deal, listing in rows:
                placement.status = "finished"
                finished += 1
                cfg = dict(placement.config_json or {})
                try:
                    duration_days = max(int(cfg.get("duration_days") or 1), 1)
                except (TypeError, ValueError):
                    duration_days = 1
                other_active = (await session.execute(
                    select(AdvertisingPlacement.id).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.id != placement.id,
                        AdvertisingPlacement.status.in_(["pending", "ready", "active"]),
                    ).limit(1)
                )).scalar_one_or_none()
                deal_finished = other_active is None
                if deal_finished:
                    deal.status = "finished_waiting_confirmation"
                    deal.finished_at = now
                notifications.append((deal.buyer_user_id, deal.seller_user_id, listing.group_title_snapshot, deal_finished, duration_days))
                await write_audit(
                    session,
                    "advertising.post_finished",
                    actor_user_id=None,
                    chat_id=listing.chat_id,
                    target_type="advertising_deal",
                    target_id=str(deal.id),
                    payload={"placement_id": placement.id, "duration_days": duration_days},
                )

    for buyer_id, seller_id, title, deal_finished, duration_days in notifications:
        buyer_text = (
            "🏁 <b>Показ рекламного поста завершён</b>\n\n"
            f"🏠 Площадка: <b>{title}</b>\n"
            f"⏳ Срок размещения: <b>{duration_days} дн.</b>\n"
            "Рекламная кампания завершена автоматически."
        )
        seller_text = (
            "🏁 <b>Размещение рекламного поста завершено</b>\n\n"
            f"🏠 Площадка: <b>{title}</b>\n"
            f"⏳ Срок: <b>{duration_days} дн.</b>"
        )
        if deal_finished:
            buyer_text += "\n\nТеперь сделку можно закрыть без претензий или открыть спор."
            seller_text += "\n\nТеперь сделку можно закрыть без претензий или открыть спор."
        for user_id, text in ((buyer_id, buyer_text), (seller_id, seller_text)):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML")
            except Exception:
                pass
    return finished


async def close_expired_no_claims_windows(session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    closed = 0
    async with session_factory() as session:
        async with session.begin():
            deals = (
                await session.execute(
                    select(AdvertisingDeal)
                    .where(
                        AdvertisingDeal.status == "finished_waiting_confirmation",
                        AdvertisingDeal.first_no_claims_at.is_not(None),
                        AdvertisingDeal.no_claims_deadline_at.is_not(None),
                        AdvertisingDeal.no_claims_deadline_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for deal in deals:
                deal.status = "completed_timeout"
                deal.completed_at = now
                await write_audit(
                    session,
                    "advertising.deal_completed_timeout",
                    actor_user_id=None,
                    target_type="advertising_deal",
                    target_id=str(deal.id),
                    payload={
                        "seller_user_id": deal.seller_user_id,
                        "buyer_user_id": deal.buyer_user_id,
                        "first_no_claims_at": deal.first_no_claims_at.isoformat() if deal.first_no_claims_at else None,
                        "deadline_at": deal.no_claims_deadline_at.isoformat() if deal.no_claims_deadline_at else None,
                    },
                )
                closed += 1
    return closed


async def advertising_lifecycle_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            published = await publish_due_posts(bot, session_factory)
            finished = await finish_expired_posts(bot, session_factory)
            closed = await close_expired_no_claims_windows(session_factory)
            if published:
                logger.info("Published %s advertising posts", published)
            if finished:
                logger.info("Finished %s advertising post placements", finished)
            if closed:
                logger.info("Closed %s advertising deals after no-claims timeout", closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Advertising lifecycle iteration failed")
        await asyncio.sleep(60)
