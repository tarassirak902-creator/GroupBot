from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing


def _listings_keyboard(rows: list[AdvertisingListing]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for listing in rows:
        kinds: list[str] = []
        if listing.offers_post:
            kinds.append("📣")
        if listing.offers_mandatory:
            kinds.append("✅")
        buttons.append([
            InlineKeyboardButton(
                text=f"{''.join(kinds)} {listing.group_title_snapshot}"[:64],
                callback_data=f"ads:listing:{listing.id}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="➕ Создать/изменить объявление", callback_data="ads:sell")])
    buttons.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def create_advertising_sales_nav_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    # Must be registered before advertising_requests: that router still contains
    # a legacy no-op ads:my_sales callback which otherwise swallows the button.
    router = Router(name="advertising_sales_nav")

    @router.callback_query(F.data == "ads:my_sales")
    async def my_sales(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer()
            return
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AdvertisingListing)
                    .where(AdvertisingListing.owner_user_id == callback.from_user.id)
                    .order_by(AdvertisingListing.updated_at.desc(), AdvertisingListing.id.desc())
                )
            ).scalars().all()

        text = (
            "📦 <b>Мои продажи</b>\n\nВыберите объявление:"
            if rows
            else "📦 <b>Мои продажи</b>\n\nУ вас пока нет рекламных объявлений."
        )
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_listings_keyboard(list(rows)),
        )
        await callback.answer()

    return router
