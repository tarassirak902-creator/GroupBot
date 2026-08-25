from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing
from groupbot.routers.advertising_edit import AdvertisingEditState


def _mode_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 По дням", callback_data=f"ads:edit:set_mode:{listing_id}:days")],
        [InlineKeyboardButton(text="👥 По подписчикам", callback_data=f"ads:edit:set_mode:{listing_id}:subscribers")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:edit:{listing_id}")],
    ])


def create_advertising_edit_actions_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    """Handle specific edit callbacks before the broad ads:edit:* handler."""
    router = Router(name="advertising_edit_actions")

    async def _owned(listing_id: int, user_id: int) -> AdvertisingListing | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(AdvertisingListing).where(
                        AdvertisingListing.id == listing_id,
                        AdvertisingListing.owner_user_id == user_id,
                    )
                )
            ).scalar_one_or_none()

    async def _start_number_edit(
        callback: CallbackQuery,
        state: FSMContext,
        *,
        state_value,
        title: str,
        prompt: str,
        require_post: bool = False,
        require_mandatory: bool = False,
    ) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _owned(listing_id, callback.from_user.id)
        if listing is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if require_post and not listing.offers_post:
            await callback.answer("Рекламные посты выключены.", show_alert=True)
            return
        if require_mandatory and not listing.offers_mandatory:
            await callback.answer("ОП выключена.", show_alert=True)
            return
        await state.set_state(state_value)
        await state.update_data(
            edit_listing_id=listing_id,
            edit_prompt_message_id=callback.message.message_id,
        )
        await callback.message.edit_text(
            f"{title}\n\n{prompt}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"ads:edit:{listing_id}")]
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:post_price:"))
    async def post_price(callback: CallbackQuery, state: FSMContext) -> None:
        await _start_number_edit(
            callback,
            state,
            state_value=AdvertisingEditState.waiting_post_price,
            title="⭐ <b>Цена рекламного поста</b>",
            prompt="Введите новую цену за сутки в Telegram Stars.",
            require_post=True,
        )

    @router.callback_query(F.data.startswith("ads:edit:post_interval:"))
    async def post_interval(callback: CallbackQuery, state: FSMContext) -> None:
        await _start_number_edit(
            callback,
            state,
            state_value=AdvertisingEditState.waiting_post_interval,
            title="⏱ <b>Интервал публикации</b>",
            prompt="Введите новый интервал в часах целым положительным числом.",
            require_post=True,
        )

    @router.callback_query(F.data.startswith("ads:edit:mandatory_price:"))
    async def mandatory_price(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _owned(listing_id, callback.from_user.id)
        if listing is None or not listing.offers_mandatory:
            await callback.answer("ОП выключена.", show_alert=True)
            return
        mode = (listing.mandatory_terms_json or {}).get("mode")
        unit = "за 1 день" if mode == "days" else "за 1 подписчика"
        await _start_number_edit(
            callback,
            state,
            state_value=AdvertisingEditState.waiting_mandatory_price,
            title="⭐ <b>Цена ОП</b>",
            prompt=f"Введите новую цену {unit} в Telegram Stars.",
            require_mandatory=True,
        )

    @router.callback_query(F.data.startswith("ads:edit:mandatory_mode:"))
    async def mandatory_mode(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _owned(listing_id, callback.from_user.id)
        if listing is None or not listing.offers_mandatory:
            await callback.answer("ОП выключена.", show_alert=True)
            return
        await callback.message.edit_text(
            "📐 <b>Способ расчёта ОП</b>\n\nВыберите новый вариант:",
            parse_mode="HTML",
            reply_markup=_mode_keyboard(listing_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:set_mode:"))
    async def set_mode(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 5:
            await callback.answer("Некорректная команда.", show_alert=True)
            return
        try:
            listing_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        mode = parts[4]
        if mode not in {"days", "subscribers"}:
            await callback.answer("Некорректный способ расчёта.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                listing = (
                    await session.execute(
                        select(AdvertisingListing).where(
                            AdvertisingListing.id == listing_id,
                            AdvertisingListing.owner_user_id == callback.from_user.id,
                            AdvertisingListing.offers_mandatory.is_(True),
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                listing.mandatory_terms_json = {
                    "mode": mode,
                    "price_unit": "day" if mode == "days" else "subscriber",
                }
        await callback.answer("Способ расчёта обновлён")
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ Способ расчёта ОП обновлён.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К редактированию", callback_data=f"ads:edit:{listing_id}")]
                ]),
            )

    @router.callback_query(F.data.startswith("ads:edit:toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                listing = (
                    await session.execute(
                        select(AdvertisingListing).where(
                            AdvertisingListing.id == listing_id,
                            AdvertisingListing.owner_user_id == callback.from_user.id,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                listing.is_active = not listing.is_active
                active = listing.is_active
        await callback.answer("Объявление включено" if active else "Объявление выключено")
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ Объявление включено." if active else "⛔ Объявление выключено.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К редактированию", callback_data=f"ads:edit:{listing_id}")]
                ]),
            )

    return router
