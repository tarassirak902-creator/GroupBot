from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing
from groupbot.routers.advertising_edit import AdvertisingEditState, _editor_keyboard, _listing_text


def _types_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Только посты", callback_data=f"ads:edit:set_types:{listing_id}:post")],
        [InlineKeyboardButton(text="✅ Только ОП", callback_data=f"ads:edit:set_types:{listing_id}:mandatory")],
        [InlineKeyboardButton(text="📣 + ✅ Посты и ОП", callback_data=f"ads:edit:set_types:{listing_id}:both")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:edit:{listing_id}")],
    ])


def _mode_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 По дням", callback_data=f"ads:edit:set_mode:{listing_id}:days")],
        [InlineKeyboardButton(text="👥 По подписчикам", callback_data=f"ads:edit:set_mode:{listing_id}:subscribers")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:edit:{listing_id}")],
    ])


def create_advertising_edit_types_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    # This router is intentionally registered before advertising_edit.
    # advertising_edit contains a broad ads:edit:* handler, so every specific
    # edit callback must be handled here first.
    router = Router(name="advertising_edit_types")

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

    async def _show_editor(callback: CallbackQuery, listing_id: int) -> None:
        if callback.message is None:
            return
        listing = await _owned(listing_id, callback.from_user.id)
        if listing is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        text = _listing_text(listing)
        text = text.replace("<b>None ⭐</b>", "<b>не настроена</b>")
        text = text.replace("<b>0 ч.</b>", "<b>не настроен</b>")
        await callback.message.edit_text(
            "✏️ <b>Редактирование объявления</b>\n\n" + text,
            parse_mode="HTML",
            reply_markup=_editor_keyboard(listing),
        )

    @router.callback_query(F.data.startswith("ads:edit:types:"))
    async def edit_types(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        if await _owned(listing_id, callback.from_user.id) is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        await callback.message.edit_text(
            "🎛 <b>Типы рекламы</b>\n\nВыберите, что будет доступно покупателям:",
            parse_mode="HTML",
            reply_markup=_types_keyboard(listing_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:set_types:"))
    async def set_types_without_defaults(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 5:
            await callback.answer("Некорректная команда.", show_alert=True)
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

        await _show_editor(callback, listing_id)
        await callback.answer("Типы рекламы обновлены")

    async def _start_number(
        callback: CallbackQuery,
        state: FSMContext,
        *,
        target_state,
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
        await state.set_state(target_state)
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
        await _start_number(
            callback, state,
            target_state=AdvertisingEditState.waiting_post_price,
            title="⭐ <b>Цена рекламного поста</b>",
            prompt="Введите новую цену за сутки в Telegram Stars.",
            require_post=True,
        )

    @router.callback_query(F.data.startswith("ads:edit:post_interval:"))
    async def post_interval(callback: CallbackQuery, state: FSMContext) -> None:
        await _start_number(
            callback, state,
            target_state=AdvertisingEditState.waiting_post_interval,
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
        await _start_number(
            callback, state,
            target_state=AdvertisingEditState.waiting_mandatory_price,
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
        await _show_editor(callback, listing_id)
        await callback.answer("Способ расчёта обновлён")

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
        await _show_editor(callback, listing_id)
        await callback.answer("Статус объявления обновлён")

    return router
