from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.routers.advertising_requests import (
    _deal_keyboard,
    _deal_text,
    _kind_text,
    _request_type_keyboard,
    _seller_request_keyboard,
)
from groupbot.services.subscriptions import active_subscription_for_group


async def _available_listing(
    session: AsyncSession,
    *,
    listing_id: int,
    buyer_user_id: int,
    lock: bool = False,
) -> AdvertisingListing | None:
    query = (
        select(AdvertisingListing, GroupOwner.user_id)
        .join(Group, Group.chat_id == AdvertisingListing.chat_id)
        .join(
            GroupOwner,
            (GroupOwner.chat_id == Group.chat_id) & GroupOwner.is_current.is_(True),
        )
        .where(
            AdvertisingListing.id == listing_id,
            AdvertisingListing.is_active.is_(True),
            AdvertisingListing.owner_user_id != buyer_user_id,
            Group.status == GroupStatus.active.value,
        )
        .limit(1)
    )
    if lock:
        query = query.with_for_update(of=AdvertisingListing)
    row = (await session.execute(query)).first()
    if row is None:
        return None
    listing, current_owner_id = row
    if int(current_owner_id) != listing.owner_user_id:
        return None
    if await active_subscription_for_group(session, listing.chat_id) is None:
        return None
    return listing


def _unavailable_text() -> str:
    return (
        "Эта рекламная площадка сейчас недоступна: объявление выключено, "
        "группа отключена, владелец сменился или подписка закончилась."
    )


def create_advertising_request_guard_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_request_guard")

    @router.callback_query(F.data.startswith("ads:request:"))
    async def choose_request_type(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        async with session_factory() as session:
            listing = await _available_listing(
                session,
                listing_id=listing_id,
                buyer_user_id=callback.from_user.id,
            )
        if listing is None:
            await callback.answer(_unavailable_text(), show_alert=True)
            return
        await callback.message.edit_text(
            "📨 <b>Отправить запрос</b>\n\nНа какой вид рекламы отправить заявку?",
            parse_mode="HTML",
            reply_markup=_request_type_keyboard(listing),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:req:type:"))
    async def send_request(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 5:
            return
        try:
            listing_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        kind = parts[4]
        if kind not in {"post", "mandatory", "both"}:
            await callback.answer("Некорректный тип рекламы.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                listing = await _available_listing(
                    session,
                    listing_id=listing_id,
                    buyer_user_id=callback.from_user.id,
                    lock=True,
                )
                if listing is None:
                    await callback.answer(_unavailable_text(), show_alert=True)
                    return

                requested_post = kind in {"post", "both"}
                requested_mandatory = kind in {"mandatory", "both"}
                if requested_post and not listing.offers_post:
                    await callback.answer("Посты в этом объявлении больше не продаются.", show_alert=True)
                    return
                if requested_mandatory and not listing.offers_mandatory:
                    await callback.answer("ОП в этом объявлении больше не продаётся.", show_alert=True)
                    return

                existing = (
                    await session.execute(
                        select(AdvertisingDeal).where(
                            AdvertisingDeal.listing_id == listing.id,
                            AdvertisingDeal.buyer_user_id == callback.from_user.id,
                            AdvertisingDeal.status == "pending",
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    await callback.answer(
                        "У вас уже есть заявка на эту площадку, ожидающая решения.",
                        show_alert=True,
                    )
                    return

                deal = AdvertisingDeal(
                    listing_id=listing.id,
                    seller_user_id=listing.owner_user_id,
                    buyer_user_id=callback.from_user.id,
                    requested_post=requested_post,
                    requested_mandatory=requested_mandatory,
                    status="pending",
                    agreed_terms_json={
                        "post_price_stars": listing.post_price_stars if requested_post else None,
                        "post_interval_minutes": listing.post_interval_minutes if requested_post else None,
                        "post_terms": listing.post_terms_json if requested_post else None,
                        "mandatory_price_stars": listing.mandatory_price_stars if requested_mandatory else None,
                        "mandatory_terms": listing.mandatory_terms_json if requested_mandatory else None,
                    },
                )
                session.add(deal)
                await session.flush()
                deal_id = deal.id
                seller_id = deal.seller_user_id
                title = listing.group_title_snapshot
                kind_label = _kind_text(deal)

        try:
            await bot.send_message(
                seller_id,
                "📥 <b>Новая рекламная заявка</b>\n\n"
                f"🏠 Площадка: <b>{title}</b>\n"
                f"👤 Покупатель: <b>{callback.from_user.full_name}</b>\n"
                f"📌 Запрос: <b>{kind_label}</b>\n\n"
                "Вы можете связаться с покупателем и обсудить условия перед принятием заявки.",
                parse_mode="HTML",
                reply_markup=_seller_request_keyboard(deal_id, callback.from_user.id),
            )
        except Exception:
            pass

        async with session_factory() as session:
            loaded = (
                await session.execute(
                    select(AdvertisingDeal, AdvertisingListing)
                    .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                    .where(AdvertisingDeal.id == deal_id)
                )
            ).first()
        if loaded is None:
            return
        deal, listing = loaded
        await callback.message.edit_text(
            "✅ <b>Заявка отправлена</b>\n\n" + _deal_text(deal, listing),
            parse_mode="HTML",
            reply_markup=_deal_keyboard(deal, callback.from_user.id),
        )
        await callback.answer("Заявка отправлена")

    return router
