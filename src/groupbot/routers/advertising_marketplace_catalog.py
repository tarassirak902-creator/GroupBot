from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingReview
from groupbot.models import Group, GroupOwner, GroupStatus, Subscription, SubscriptionStatus


def _short_title(value: str, limit: int = 38) -> str:
    text = (value or "Группа").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _rating_label(rating: float | None) -> str:
    if rating is None:
        return "⭐ Нет оценок"
    return f"⭐ {rating:.1f}"


def _offer_label(listing: AdvertisingListing) -> str:
    variants: list[str] = []
    if listing.offers_mandatory:
        variants.append("ОП")
    if listing.offers_post:
        variants.append("Пост")
    return f"Варианты: {' / '.join(variants)}"[:64]


def _catalog_keyboard(rows: list[AdvertisingListing], ratings: dict[int, float]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for listing in rows:
        target = f"ads:listing:{listing.id}"
        buttons.extend([
            [InlineKeyboardButton(text=f"🏷️ Объявление · {_rating_label(ratings.get(listing.id))} 👇"[:64], callback_data=target)],
            [InlineKeyboardButton(text=f"🏠 {_short_title(listing.group_title_snapshot)}"[:64], callback_data=target)],
            [InlineKeyboardButton(text=_offer_label(listing), callback_data=target)],
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def create_advertising_marketplace_catalog_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_marketplace_catalog")

    @router.callback_query(F.data == "ads:buy")
    async def buy_advertising(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        await state.clear()
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            rows = list((await session.execute(
                select(AdvertisingListing)
                .join(Group, Group.chat_id == AdvertisingListing.chat_id)
                .join(
                    GroupOwner,
                    (GroupOwner.chat_id == AdvertisingListing.chat_id)
                    & (GroupOwner.user_id == AdvertisingListing.owner_user_id)
                    & (GroupOwner.is_current.is_(True)),
                )
                .join(
                    Subscription,
                    (Subscription.owner_user_id == AdvertisingListing.owner_user_id)
                    & (Subscription.status == SubscriptionStatus.active.value)
                    & (Subscription.ends_at > now),
                )
                .where(
                    AdvertisingListing.is_active.is_(True),
                    AdvertisingListing.owner_user_id != callback.from_user.id,
                    Group.status == GroupStatus.active.value,
                )
                .distinct(AdvertisingListing.id)
                .order_by(AdvertisingListing.id, AdvertisingListing.updated_at.desc())
                .limit(50)
            )).scalars().all())
            ratings: dict[int, float] = {}
            if rows:
                listing_ids = [row.id for row in rows]
                rating_rows = (await session.execute(
                    select(AdvertisingDeal.listing_id, func.avg(AdvertisingReview.rating))
                    .join(AdvertisingReview, AdvertisingReview.deal_id == AdvertisingDeal.id)
                    .where(
                        AdvertisingDeal.listing_id.in_(listing_ids),
                        AdvertisingReview.reviewed_user_id == AdvertisingDeal.seller_user_id,
                    )
                    .group_by(AdvertisingDeal.listing_id)
                )).all()
                ratings = {int(listing_id): float(avg_rating) for listing_id, avg_rating in rating_rows if avg_rating is not None}
        if not rows:
            await callback.message.edit_text(
                "🛒 <b>Купить рекламу</b>\n\nАктивных объявлений других рекламодателей пока нет.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")]]),
            )
        else:
            await callback.message.edit_text(
                "🛒 <b>Купить рекламу</b>\n\nВыберите рекламную площадку. Цена и подробные условия указаны внутри объявления:",
                parse_mode="HTML",
                reply_markup=_catalog_keyboard(rows, ratings),
            )
        await callback.answer()

    return router
