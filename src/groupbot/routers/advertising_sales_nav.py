from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing


def _kind_text(deal: AdvertisingDeal) -> str:
    if deal.requested_post and deal.requested_mandatory:
        return "📣+✅"
    if deal.requested_post:
        return "📣"
    return "✅"


def _hub_keyboard(listing_count: int, pending_count: int, active_count: int, history_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📣 Мои объявления · {listing_count}", callback_data="ads:sales:listings")],
        [InlineKeyboardButton(text=f"📥 Входящие заявки · {pending_count}", callback_data="ads:sales:incoming")],
        [InlineKeyboardButton(text=f"💼 Активные продажи · {active_count}", callback_data="ads:sales:active")],
        [InlineKeyboardButton(text=f"✅ История продаж · {history_count}", callback_data="ads:sales:history")],
        [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
    ])


def _listings_keyboard(rows: list[AdvertisingListing]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for listing in rows:
        kinds: list[str] = []
        if listing.offers_post:
            kinds.append("📣")
        if listing.offers_mandatory:
            kinds.append("✅")
        buttons.append([InlineKeyboardButton(text=f"{''.join(kinds)} {listing.group_title_snapshot}"[:64], callback_data=f"ads:listing:{listing.id}")])
    buttons.append([InlineKeyboardButton(text="➕ Создать/изменить объявление", callback_data="ads:sell")])
    buttons.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _deals_keyboard(rows: list[tuple[AdvertisingDeal, AdvertisingListing]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for deal, listing in rows:
        buttons.append([InlineKeyboardButton(text=f"{_kind_text(deal)} {listing.group_title_snapshot}"[:64], callback_data=f"ads:deal:{deal.id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def create_advertising_sales_nav_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_sales_nav")

    async def _load_counts(user_id: int) -> tuple[int, int, int, int]:
        async with session_factory() as session:
            listings = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.owner_user_id == user_id))).scalars().all()
            deals = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.seller_user_id == user_id))).scalars().all()
        pending = sum(1 for d in deals if d.status == "pending")
        active = sum(1 for d in deals if d.status == "accepted" or (d.started_at is not None and d.completed_at is None and d.status not in {"cancelled", "rejected"}))
        history = max(len(deals) - pending - active, 0)
        return len(listings), pending, active, history

    async def _load_deals(user_id: int, section: str) -> list[tuple[AdvertisingDeal, AdvertisingListing]]:
        async with session_factory() as session:
            rows = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(AdvertisingDeal.seller_user_id == user_id)
                .order_by(AdvertisingDeal.created_at.desc())
                .limit(100)
            )).all()
        if section == "incoming":
            return [(d, l) for d, l in rows if d.status == "pending"]
        if section == "active":
            return [(d, l) for d, l in rows if d.status == "accepted" or (d.started_at is not None and d.completed_at is None and d.status not in {"cancelled", "rejected"})]
        return [(d, l) for d, l in rows if d.status not in {"pending", "accepted"} and not (d.started_at is not None and d.completed_at is None and d.status not in {"cancelled", "rejected"})]

    @router.callback_query(F.data == "ads:my_sales")
    async def my_sales(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer(); return
        listing_count, pending_count, active_count, history_count = await _load_counts(callback.from_user.id)
        await callback.message.edit_text(
            "📦 <b>Мои продажи</b>\n\nЗдесь находятся ваши рекламные объявления, входящие заявки и все сделки по продаже рекламы.",
            parse_mode="HTML",
            reply_markup=_hub_keyboard(listing_count, pending_count, active_count, history_count),
        )
        await callback.answer()

    @router.callback_query(F.data == "ads:sales:listings")
    async def sales_listings(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer(); return
        async with session_factory() as session:
            rows = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.owner_user_id == callback.from_user.id).order_by(AdvertisingListing.updated_at.desc(), AdvertisingListing.id.desc()))).scalars().all()
        text = "📣 <b>Мои объявления</b>\n\nВыберите рекламную площадку:" if rows else "📣 <b>Мои объявления</b>\n\nУ вас пока нет рекламных объявлений."
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_listings_keyboard(list(rows)))
        await callback.answer()

    async def _show_deal_section(callback: CallbackQuery, section: str, title: str, empty: str) -> None:
        if callback.message is None:
            await callback.answer(); return
        rows = await _load_deals(callback.from_user.id, section)
        text = f"{title}\n\nВыберите сделку:" if rows else f"{title}\n\n{empty}"
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_deals_keyboard(rows))
        await callback.answer()

    @router.callback_query(F.data == "ads:sales:incoming")
    async def incoming(callback: CallbackQuery) -> None:
        await _show_deal_section(callback, "incoming", "📥 <b>Входящие заявки</b>", "Новых заявок сейчас нет.")

    @router.callback_query(F.data == "ads:sales:active")
    async def active(callback: CallbackQuery) -> None:
        await _show_deal_section(callback, "active", "💼 <b>Активные продажи</b>", "Активных продаж сейчас нет.")

    @router.callback_query(F.data == "ads:sales:history")
    async def history(callback: CallbackQuery) -> None:
        await _show_deal_section(callback, "history", "✅ <b>История продаж</b>", "История продаж пока пуста.")

    return router
