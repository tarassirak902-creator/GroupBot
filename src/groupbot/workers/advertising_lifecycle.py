from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.audit import write_audit
from groupbot.services.subscriptions import active_subscription_for_group
from groupbot.workers.advertising_mutual_lifecycle import process_mutual_op

logger = logging.getLogger(__name__)


def _post_keyboard(cfg: dict) -> InlineKeyboardMarkup | None:
    text = str(cfg.get("button_text") or "").strip()
    url = str(cfg.get("button_url") or "").strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text[:64], url=url)]])


def _settlement_keyboard(deal_id: int, *, allow_dispute: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✅ Претензий нет", callback_data=f"ads:settle:ok:{deal_id}")]]
    if allow_dispute:
        rows.append([InlineKeyboardButton(text="⚠️ Открыть спор", callback_data=f"ads:settle:dispute:{deal_id}")])
    rows.append([InlineKeyboardButton(text="⭐ Оставить отзыв", callback_data=f"ads:settle:review:{deal_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _finish_deal_if_no_other_active(session: AsyncSession, deal: AdvertisingDeal, placement_id: int, now: datetime) -> bool:
    other_active = (await session.execute(
        select(AdvertisingPlacement.id).where(
            AdvertisingPlacement.deal_id == deal.id,
            AdvertisingPlacement.id != placement_id,
            AdvertisingPlacement.status.in_(["pending", "ready", "active"]),
        ).limit(1)
    )).scalar_one_or_none()
    if other_active is not None:
        return False
    deal.status = "finished_waiting_confirmation"
    deal.finished_at = now
    return True


async def _platform_unavailable_reason(
    session: AsyncSession,
    *,
    listing: AdvertisingListing,
    seller_user_id: int,
) -> str | None:
    owner_row = (
        await session.execute(
            select(Group.status, GroupOwner.user_id)
            .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
            .where(
                Group.chat_id == listing.chat_id,
                GroupOwner.is_current.is_(True),
            )
            .limit(1)
        )
    ).first()
    if owner_row is None:
        return "у рекламной группы больше нет текущего владельца в Mimorus"
    if int(owner_row.user_id) != seller_user_id:
        return "у рекламной группы сменился владелец"
    if owner_row.status != GroupStatus.active.value:
        return "рекламная группа больше не подключена к Mimorus"
    if await active_subscription_for_group(session, listing.chat_id) is None:
        return "у владельца рекламной группы закончилась активная подписка Mimorus"
    return None


async def fail_unavailable_platforms(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    failed = 0
    notifications: list[tuple[int, int, int, str, str]] = []
    async with session_factory() as session:
        async with session.begin():
            rows = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(AdvertisingDeal.status == "accepted")
                .with_for_update(skip_locked=True)
            )).all()
            for deal, listing in rows:
                reason = await _platform_unavailable_reason(
                    session,
                    listing=listing,
                    seller_user_id=deal.seller_user_id,
                )
                if reason is None:
                    continue
                placements = list((await session.execute(
                    select(AdvertisingPlacement)
                    .where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.status.in_(["pending", "ready", "active"]),
                    )
                    .with_for_update()
                )).scalars().all())
                if not placements:
                    continue
                for placement in placements:
                    placement.status = "failed"
                    cfg = dict(placement.config_json or {})
                    cfg["failure_reason"] = reason
                    cfg["failed_at"] = now.isoformat()
                    placement.config_json = cfg
                deal.status = "finished_waiting_confirmation"
                deal.finished_at = now
                failed += 1
                notifications.append((deal.id, deal.buyer_user_id, deal.seller_user_id, listing.group_title_snapshot, reason))
                await write_audit(
                    session,
                    "advertising.deal_failed_platform_unavailable",
                    actor_user_id=None,
                    chat_id=listing.chat_id,
                    target_type="advertising_deal",
                    target_id=str(deal.id),
                    payload={
                        "reason": reason,
                        "placement_ids": [placement.id for placement in placements],
                    },
                )

    for deal_id, buyer_id, seller_id, title, reason in notifications:
        buyer_text = (
            "⚠️ <b>Рекламное размещение остановлено</b>\n\n"
            f"🏠 Площадка: <b>{escape(title)}</b>\n"
            f"Причина: <b>{escape(reason)}</b>\n\n"
            "Mimorus не отмечает такую сделку как успешно выполненную. Вы можете подтвердить отсутствие претензий или открыть спор."
        )
        seller_text = (
            "⚠️ <b>Рекламное размещение остановлено</b>\n\n"
            f"🏠 Ваша площадка: <b>{escape(title)}</b>\n"
            f"Причина: <b>{escape(reason)}</b>\n\n"
            "Сделка переведена на подтверждение сторон. Вы можете подтвердить отсутствие претензий или открыть спор."
        )
        markup = _settlement_keyboard(deal_id)
        for user_id, text in ((buyer_id, buyer_text), (seller_id, seller_text)):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
    return failed


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
                    placement.last_published_at = now
                    published += 1
                    await write_audit(session, "advertising.post_published", actor_user_id=None, chat_id=listing.chat_id, target_type="advertising_deal", target_id=str(deal.id), payload={"placement_id": placement.id})
                except Exception:
                    logger.exception("Failed to publish advertising post deal=%s chat=%s", deal.id, listing.chat_id)
    return published


async def finish_expired_posts(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    notifications: list[tuple[int, int, int, str, bool, int]] = []
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
                deal_finished = await _finish_deal_if_no_other_active(session, deal, placement.id, now)
                notifications.append((deal.id, deal.buyer_user_id, deal.seller_user_id, listing.group_title_snapshot, deal_finished, duration_days))
                await write_audit(session, "advertising.post_finished", actor_user_id=None, chat_id=listing.chat_id, target_type="advertising_deal", target_id=str(deal.id), payload={"placement_id": placement.id, "duration_days": duration_days})

    for deal_id, buyer_id, seller_id, title, deal_finished, duration_days in notifications:
        buyer_text = (
            "🏁 <b>Показ рекламного поста завершён</b>\n\n"
            f"🏠 Площадка: <b>{escape(title)}</b>\n"
            f"⏳ Срок размещения: <b>{duration_days} дн.</b>\n"
            "Рекламная кампания завершена автоматически."
        )
        seller_text = (
            "🏁 <b>Размещение рекламного поста завершено</b>\n\n"
            f"🏠 Площадка: <b>{escape(title)}</b>\n"
            f"⏳ Срок: <b>{duration_days} дн.</b>"
        )
        markup = None
        if deal_finished:
            buyer_text += "\n\nВыберите действие по сделке:"
            seller_text += "\n\nВыберите действие по сделке:"
            markup = _settlement_keyboard(deal_id)
        for user_id, text in ((buyer_id, buyer_text), (seller_id, seller_text)):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
    return finished


async def finish_due_mandatory(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    notifications: list[tuple[int, int, int, str, str, int, bool, str]] = []
    finished = 0
    async with session_factory() as session:
        async with session.begin():
            rows = (await session.execute(
                select(AdvertisingPlacement, AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingDeal, AdvertisingDeal.id == AdvertisingPlacement.deal_id)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(
                    AdvertisingPlacement.kind == "mandatory",
                    AdvertisingPlacement.status == "active",
                )
                .with_for_update(skip_locked=True)
            )).all()
            for placement, deal, listing in rows:
                cfg = dict(placement.config_json or {})
                mode = str(cfg.get("mode") or "days")
                try:
                    quantity = max(int(cfg.get("quantity") or 1), 1)
                except (TypeError, ValueError):
                    quantity = 1
                should_finish = False
                result_text = ""
                if mode == "days":
                    should_finish = placement.ends_at is not None and placement.ends_at <= now
                    result_text = f"{quantity} дн."
                elif mode == "subscribers":
                    target_chat_id = cfg.get("target_chat_id")
                    target_count = cfg.get("target_member_count")
                    baseline = cfg.get("baseline_member_count")
                    if not isinstance(target_chat_id, int) or not isinstance(target_count, int):
                        continue
                    try:
                        current_count = await bot.get_chat_member_count(target_chat_id)
                    except Exception:
                        logger.exception("Could not check OP subscriber target deal=%s target_chat=%s", deal.id, target_chat_id)
                        continue
                    cfg["last_member_count"] = current_count
                    placement.config_json = cfg
                    should_finish = current_count >= target_count
                    gained = max(current_count - int(baseline or current_count), 0)
                    result_text = f"{gained}/{quantity} подписчиков"
                if not should_finish:
                    continue

                placement.status = "finished"
                finished += 1
                deal_finished = await _finish_deal_if_no_other_active(session, deal, placement.id, now)
                target_title = str(cfg.get("target_title") or "Группа")
                notifications.append((deal.id, deal.buyer_user_id, deal.seller_user_id, listing.group_title_snapshot, mode, quantity, deal_finished, target_title))
                await write_audit(
                    session,
                    "advertising.mandatory_finished",
                    actor_user_id=None,
                    chat_id=listing.chat_id,
                    target_type="advertising_deal",
                    target_id=str(deal.id),
                    payload={"placement_id": placement.id, "mode": mode, "quantity": quantity, "result": result_text},
                )

    for deal_id, buyer_id, seller_id, seller_group, mode, quantity, deal_finished, target_title in notifications:
        volume = f"{quantity} дн." if mode == "days" else f"{quantity} подписчиков"
        buyer_text = (
            "🏁 <b>Обязательная подписка завершена</b>\n\n"
            f"🏠 Площадка рекламодателя: <b>{escape(seller_group)}</b>\n"
            f"🎯 Ваша площадка: <b>{escape(target_title)}</b>\n"
            f"📐 Выполненный объём: <b>{volume}</b>"
        )
        seller_text = (
            "🏁 <b>Размещение ОП завершено</b>\n\n"
            f"🏠 Ваша площадка: <b>{escape(seller_group)}</b>\n"
            f"🎯 ОП на: <b>{escape(target_title)}</b>\n"
            f"📐 Объём: <b>{volume}</b>"
        )
        markup = None
        if deal_finished:
            buyer_text += "\n\nВыберите действие по сделке:"
            seller_text += "\n\nВыберите действие по сделке:"
            markup = _settlement_keyboard(deal_id)
        for user_id, text in ((buyer_id, buyer_text), (seller_id, seller_text)):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
    return finished


async def close_expired_no_claims_windows(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    closed_rows: list[tuple[int, int, int]] = []
    async with session_factory() as session:
        async with session.begin():
            deals = (await session.execute(
                select(AdvertisingDeal).where(
                    AdvertisingDeal.status == "finished_waiting_confirmation",
                    AdvertisingDeal.first_no_claims_at.is_not(None),
                    AdvertisingDeal.no_claims_deadline_at.is_not(None),
                    AdvertisingDeal.no_claims_deadline_at <= now,
                ).with_for_update(skip_locked=True)
            )).scalars().all()
            for deal in deals:
                deal.status = "completed_timeout"
                deal.completed_at = now
                closed_rows.append((deal.id, deal.buyer_user_id, deal.seller_user_id))
                await write_audit(session, "advertising.deal_completed_timeout", actor_user_id=None, target_type="advertising_deal", target_id=str(deal.id), payload={
                    "seller_user_id": deal.seller_user_id,
                    "buyer_user_id": deal.buyer_user_id,
                    "first_no_claims_at": deal.first_no_claims_at.isoformat() if deal.first_no_claims_at else None,
                    "deadline_at": deal.no_claims_deadline_at.isoformat() if deal.no_claims_deadline_at else None,
                })
    for deal_id, buyer_id, seller_id in closed_rows:
        text = (
            "✅ <b>Рекламная сделка закрыта автоматически</b>\n\n"
            "Одна сторона подтвердила отсутствие претензий, а вторая не выбрала действие в течение 5 часов.\n\n"
            "Спор по закрытой сделке открыть уже нельзя, но отзыв и оценку оставить можно."
        )
        markup = _settlement_keyboard(deal_id, allow_dispute=False)
        for user_id in (buyer_id, seller_id):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
    return len(closed_rows)


async def advertising_lifecycle_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            failed = await fail_unavailable_platforms(bot, session_factory)
            published = await publish_due_posts(bot, session_factory)
            finished_posts = await finish_expired_posts(bot, session_factory)
            finished_op = await finish_due_mandatory(bot, session_factory)
            finished_mutual = await process_mutual_op(bot, session_factory)
            closed = await close_expired_no_claims_windows(bot, session_factory)
            if failed:
                logger.info("Failed %s advertising deals because platform became unavailable", failed)
            if published:
                logger.info("Published %s advertising posts", published)
            if finished_posts:
                logger.info("Finished %s advertising post placements", finished_posts)
            if finished_op:
                logger.info("Finished %s advertising OP placements", finished_op)
            if finished_mutual:
                logger.info("Finished %s mutual OP directions", finished_mutual)
            if closed:
                logger.info("Closed %s advertising deals after no-claims timeout", closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Advertising lifecycle iteration failed")
        await asyncio.sleep(30)
