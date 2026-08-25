from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement

POST_DAILY_LIMIT = 1
MANDATORY_DAILY_LIMIT = 3


def _utc_day_start(now: datetime) -> datetime:
    current = now.astimezone(timezone.utc)
    return datetime(current.year, current.month, current.day, tzinfo=timezone.utc)


def _kind_text(deal: AdvertisingDeal) -> str:
    if deal.requested_post and deal.requested_mandatory:
        return "📣 Рекламный пост + ✅ ОП"
    if deal.requested_post:
        return "📣 Рекламный пост"
    return "✅ Обязательная подписка"


def _duration_days(deal: AdvertisingDeal) -> int:
    terms = (deal.agreed_terms_json or {}).get("post_terms") or {}
    try:
        return max(int(terms.get("duration_days") or 1), 1)
    except (TypeError, ValueError):
        return 1


def _after_action_keyboard(deal: AdvertisingDeal, viewer_id: int) -> InlineKeyboardMarkup:
    other_id = deal.buyer_user_id if viewer_id == deal.seller_user_id else deal.seller_user_id
    rows = [[InlineKeyboardButton(text="💬 Связаться", url=f"tg://user?id={other_id}")]]
    if viewer_id == deal.buyer_user_id and deal.status == "accepted" and deal.requested_mandatory:
        rows.append([InlineKeyboardButton(text="📦 Передать материалы ОП", callback_data=f"ads:materials:{deal.id}")])
    if viewer_id == deal.buyer_user_id:
        rows.append([InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_advertising_deal_actions_v2_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_deal_actions_v2")

    @router.callback_query(F.data.startswith("ads:deal:accept:"))
    async def accept_deal(callback: CallbackQuery, bot: Bot) -> None:
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная заявка.", show_alert=True)
            return
        now = datetime.now(timezone.utc)
        day_start = _utc_day_start(now)
        accepted_deal = None
        buyer_id = None
        title = ""
        kind_label = ""
        post_started = False
        duration_days = 1
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None:
                    await callback.answer("Заявка не найдена.", show_alert=True); return
                if callback.from_user.id != deal.seller_user_id:
                    await callback.answer("Принять заявку может только рекламодатель.", show_alert=True); return
                if deal.status != "pending":
                    await callback.answer("Эта заявка уже обработана.", show_alert=True); return
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id).with_for_update())).scalar_one_or_none()
                if listing is None or listing.owner_user_id != callback.from_user.id:
                    await callback.answer("Рекламное объявление недоступно.", show_alert=True); return
                if deal.requested_post:
                    used_post = int((await session.execute(select(func.count()).select_from(AdvertisingDeal).where(AdvertisingDeal.listing_id == deal.listing_id, AdvertisingDeal.accepted_at >= day_start, AdvertisingDeal.requested_post.is_(True)))).scalar_one())
                    if used_post >= POST_DAILY_LIMIT:
                        await callback.answer("Лимит рекламных постов на сегодня уже использован: 1 из 1.", show_alert=True); return
                if deal.requested_mandatory:
                    used_mandatory = int((await session.execute(select(func.count()).select_from(AdvertisingDeal).where(AdvertisingDeal.listing_id == deal.listing_id, AdvertisingDeal.accepted_at >= day_start, AdvertisingDeal.requested_mandatory.is_(True)))).scalar_one())
                    if used_mandatory >= MANDATORY_DAILY_LIMIT:
                        await callback.answer("Лимит ОП на сегодня уже использован: 3 из 3.", show_alert=True); return
                deal.status = "accepted"
                deal.accepted_at = now
                duration_days = _duration_days(deal)
                if deal.requested_post:
                    post = (await session.execute(select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id, AdvertisingPlacement.kind == "post").with_for_update())).scalar_one_or_none()
                    if post is None:
                        post = AdvertisingPlacement(deal_id=deal.id, kind="post", status="pending", config_json={"price_stars": (deal.agreed_terms_json or {}).get("post_price_stars"), "interval_minutes": (deal.agreed_terms_json or {}).get("post_interval_minutes"), "terms": (deal.agreed_terms_json or {}).get("post_terms")})
                        session.add(post)
                    elif post.status == "ready":
                        post.status = "active"
                        post.starts_at = now
                        post.ends_at = now + timedelta(days=duration_days)
                        cfg = dict(post.config_json or {})
                        cfg["duration_days"] = duration_days
                        cfg["total_price_stars"] = int((deal.agreed_terms_json or {}).get("post_price_stars") or 0) * duration_days
                        post.config_json = cfg
                        deal.started_at = now
                        post_started = True
                if deal.requested_mandatory:
                    mandatory = (await session.execute(select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id, AdvertisingPlacement.kind == "mandatory").with_for_update())).scalar_one_or_none()
                    if mandatory is None:
                        session.add(AdvertisingPlacement(deal_id=deal.id, kind="mandatory", status="pending", config_json={"price_stars": (deal.agreed_terms_json or {}).get("mandatory_price_stars"), "terms": (deal.agreed_terms_json or {}).get("mandatory_terms")}))
                buyer_id = deal.buyer_user_id
                title = listing.group_title_snapshot
                kind_label = _kind_text(deal)
                accepted_deal = deal
        if accepted_deal is None or buyer_id is None:
            return
        try:
            if post_started:
                text = f"✅ <b>Рекламодатель одобрил ваш рекламный пост</b>\n\n🏠 Площадка: <b>{title}</b>\n⏳ Срок: <b>{duration_days} дн.</b>\n\n🚀 Показ поста запущен автоматически. Mimorus сообщит, когда размещение завершится."
            else:
                text = f"✅ <b>Рекламодатель принял вашу заявку</b>\n\n🏠 Площадка: <b>{title}</b>\n📌 Формат: <b>{kind_label}</b>\n\nТеперь передайте материалы для запуска ОП внутри Mimorus."
            await bot.send_message(buyer_id, text, parse_mode="HTML", reply_markup=_after_action_keyboard(accepted_deal, buyer_id))
        except Exception:
            pass
        if callback.message is not None:
            status = f"🚀 <b>Рекламный пост запущен на {duration_days} дн.</b>" if post_started else "Статус: <b>ожидает материалов покупателя</b>"
            await callback.message.edit_text(f"✅ <b>Заявка принята</b>\n\n🏠 Площадка: <b>{title}</b>\n📌 Формат: <b>{kind_label}</b>\n{status}\n\nЛимиты учитываются с момента принятия заявки.", parse_mode="HTML", reply_markup=_after_action_keyboard(accepted_deal, callback.from_user.id))
        await callback.answer("Заявка принята")

    @router.callback_query(F.data.startswith("ads:deal:reject:"))
    async def reject_deal(callback: CallbackQuery, bot: Bot) -> None:
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная заявка.", show_alert=True); return
        now = datetime.now(timezone.utc)
        buyer_id = None
        title = ""
        kind_label = ""
        rejected_deal = None
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None:
                    await callback.answer("Заявка не найдена.", show_alert=True); return
                if callback.from_user.id != deal.seller_user_id:
                    await callback.answer("Отклонить заявку может только рекламодатель.", show_alert=True); return
                if deal.status != "pending":
                    await callback.answer("Эта заявка уже обработана.", show_alert=True); return
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id))).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Объявление не найдено.", show_alert=True); return
                deal.status = "rejected"
                deal.rejected_at = now
                for placement in (await session.execute(select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id))).scalars().all():
                    if placement.status in {"draft", "ready", "pending"}:
                        placement.status = "rejected"
                buyer_id = deal.buyer_user_id
                title = listing.group_title_snapshot
                kind_label = _kind_text(deal)
                rejected_deal = deal
        if rejected_deal is None or buyer_id is None:
            return
        try:
            await bot.send_message(buyer_id, f"❌ <b>Рекламодатель отклонил заявку</b>\n\n🏠 Площадка: <b>{title}</b>\n📌 Формат: <b>{kind_label}</b>", parse_mode="HTML", reply_markup=_after_action_keyboard(rejected_deal, buyer_id))
        except Exception:
            pass
        if callback.message is not None:
            await callback.message.edit_text(f"❌ <b>Заявка отклонена</b>\n\n🏠 Площадка: <b>{title}</b>\n📌 Формат: <b>{kind_label}</b>", parse_mode="HTML", reply_markup=_after_action_keyboard(rejected_deal, callback.from_user.id))
        await callback.answer("Заявка отклонена")

    return router
