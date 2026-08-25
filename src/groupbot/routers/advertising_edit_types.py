from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing
from groupbot.routers.advertising_edit import _editor_keyboard, _listing_text


def create_advertising_edit_types_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_edit_types")

    @router.callback_query(F.data.startswith("ads:edit:set_types:"))
    async def set_types_without_defaults(callback: CallbackQuery) -> None:
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
            return

        async with session_factory() as session:
            async with session.begin():
                listing = (
                    await session.execute(
                        select(AdvertisingListing)
                        .where(
                            AdvertisingListing.id == listing_id,
                            AdvertisingListing.owner_user_id == callback.from_user.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return

                old_post = listing.offers_post
                old_mandatory = listing.offers_mandatory
                listing.offers_post = kind in {"post", "both"}
                listing.offers_mandatory = kind in {"mandatory", "both"}

                if not listing.offers_post:
                    listing.post_price_stars = None
                    listing.post_interval_minutes = None
                    listing.post_terms_json = None
                elif not old_post:
                    listing.post_price_stars = None
                    listing.post_interval_minutes = None
                    listing.post_terms_json = {"price_period": "day"}

                if not listing.offers_mandatory:
                    listing.mandatory_price_stars = None
                    listing.mandatory_terms_json = None
                elif not old_mandatory:
                    listing.mandatory_price_stars = None
                    listing.mandatory_terms_json = {"mode": "days", "price_unit": "day"}

        async with session_factory() as session:
            listing = (
                await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == listing_id)
                )
            ).scalar_one()

        text = _listing_text(listing)
        text = text.replace("<b>None ⭐</b>", "<b>не настроена</b>")
        text = text.replace("<b>0 ч.</b>", "<b>не настроен</b>")
        await callback.message.edit_text(
            "✏️ <b>Редактирование объявления</b>\n\n" + text,
            parse_mode="HTML",
            reply_markup=_editor_keyboard(listing),
        )
        await callback.answer("Типы рекламы обновлены")

    return router
