from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing


class AdvertisingEditState(StatesGroup):
    waiting_post_price = State()
    waiting_post_interval = State()
    waiting_mandatory_price = State()


def _listing_text(listing: AdvertisingListing) -> str:
    members = f"{listing.member_count_snapshot:,}".replace(",", " ")
    lines = [
        "📢 <b>Рекламное объявление</b>",
        "",
        f"🏠 Группа: <b>{listing.group_title_snapshot}</b>",
        f"👥 Участников: <b>{members}</b>",
        f"📌 Статус: <b>{'активно' if listing.is_active else 'выключено'}</b>",
        "",
    ]
    if listing.offers_post:
        interval = listing.post_interval_minutes or 0
        hours = interval / 60 if interval else 0
        hours_text = str(int(hours)) if float(hours).is_integer() else str(hours)
        lines.extend([
            "📣 <b>Рекламные посты</b>",
            f"⭐ Цена за сутки: <b>{listing.post_price_stars} ⭐</b>",
            f"⏱ Интервал публикации: <b>{hours_text} ч.</b>",
            "",
        ])
    if listing.offers_mandatory:
        terms = listing.mandatory_terms_json or {}
        mode = terms.get("mode")
        unit = "за 1 день" if mode == "days" else "за 1 подписчика"
        lines.extend([
            "✅ <b>Обязательная подписка</b>",
            f"📐 Расчёт: <b>{'по дням' if mode == 'days' else 'по количеству подписчиков'}</b>",
            f"⭐ Цена {unit}: <b>{listing.mandatory_price_stars} ⭐</b>",
        ])
    return "\n".join(lines)


def _own_listing_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"ads:edit:{listing_id}")],
        [InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")],
        [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
    ])


def _other_listing_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить запрос", callback_data=f"ads:request:{listing_id}")],
        [InlineKeyboardButton(text="◀️ Купить рекламу", callback_data="ads:buy")],
        [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
    ])


def _editor_keyboard(listing: AdvertisingListing) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎛 Типы рекламы", callback_data=f"ads:edit:types:{listing.id}")],
    ]
    if listing.offers_post:
        rows.extend([
            [InlineKeyboardButton(text="⭐ Цена поста", callback_data=f"ads:edit:post_price:{listing.id}")],
            [InlineKeyboardButton(text="⏱ Интервал поста", callback_data=f"ads:edit:post_interval:{listing.id}")],
        ])
    if listing.offers_mandatory:
        rows.extend([
            [InlineKeyboardButton(text="📐 Способ расчёта ОП", callback_data=f"ads:edit:mandatory_mode:{listing.id}")],
            [InlineKeyboardButton(text="⭐ Цена ОП", callback_data=f"ads:edit:mandatory_price:{listing.id}")],
        ])
    rows.append([
        InlineKeyboardButton(
            text="⛔ Выключить объявление" if listing.is_active else "✅ Включить объявление",
            callback_data=f"ads:edit:toggle:{listing.id}",
        )
    ])
    rows.append([InlineKeyboardButton(text="◀️ К объявлению", callback_data=f"ads:listing:{listing.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _types_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Только посты", callback_data=f"ads:edit:set_types:{listing_id}:post")],
        [InlineKeyboardButton(text="✅ Только ОП", callback_data=f"ads:edit:set_types:{listing_id}:mandatory")],
        [InlineKeyboardButton(text="📣 + ✅ Посты и ОП", callback_data=f"ads:edit:set_types:{listing_id}:both")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:edit:{listing_id}")],
    ])


def _mandatory_mode_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 По дням", callback_data=f"ads:edit:set_mode:{listing_id}:days")],
        [InlineKeyboardButton(text="👥 По подписчикам", callback_data=f"ads:edit:set_mode:{listing_id}:subscribers")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:edit:{listing_id}")],
    ])


def create_advertising_edit_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_edit")

    async def _get_listing(listing_id: int) -> AdvertisingListing | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == listing_id)
                )
            ).scalar_one_or_none()

    async def _owned_listing(listing_id: int, user_id: int) -> AdvertisingListing | None:
        listing = await _get_listing(listing_id)
        if listing is None or listing.owner_user_id != user_id:
            return None
        return listing

    async def _render_editor(callback: CallbackQuery, listing: AdvertisingListing) -> None:
        if callback.message is None:
            return
        await callback.message.edit_text(
            "✏️ <b>Редактирование объявления</b>\n\n" + _listing_text(listing),
            parse_mode="HTML",
            reply_markup=_editor_keyboard(listing),
        )

    @router.callback_query(F.data.startswith("ads:listing:"))
    async def open_listing(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _get_listing(listing_id)
        if listing is None or (not listing.is_active and listing.owner_user_id != callback.from_user.id):
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        own = listing.owner_user_id == callback.from_user.id
        await callback.message.edit_text(
            _listing_text(listing),
            parse_mode="HTML",
            reply_markup=_own_listing_keyboard(listing.id) if own else _other_listing_keyboard(listing.id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:"))
    async def edit_listing(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            return
        try:
            listing_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _owned_listing(listing_id, callback.from_user.id)
        if listing is None:
            await callback.answer("Редактировать это объявление нельзя.", show_alert=True)
            return
        await state.clear()
        await _render_editor(callback, listing)
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:types:"))
    async def edit_types(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _owned_listing(listing_id, callback.from_user.id)
        if listing is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        await callback.message.edit_text(
            "🎛 <b>Типы рекламы</b>\n\nВыберите, что будет доступно покупателям:",
            parse_mode="HTML",
            reply_markup=_types_keyboard(listing_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:set_types:"))
    async def set_types(callback: CallbackQuery) -> None:
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
                listing.offers_post = kind in {"post", "both"}
                listing.offers_mandatory = kind in {"mandatory", "both"}
                if listing.offers_post:
                    if listing.post_price_stars is None:
                        listing.post_price_stars = 1
                    if listing.post_interval_minutes is None:
                        listing.post_interval_minutes = 60
                    if listing.post_terms_json is None:
                        listing.post_terms_json = {"price_period": "day"}
                else:
                    listing.post_price_stars = None
                    listing.post_interval_minutes = None
                    listing.post_terms_json = None
                if listing.offers_mandatory:
                    if listing.mandatory_price_stars is None:
                        listing.mandatory_price_stars = 1
                    if listing.mandatory_terms_json is None:
                        listing.mandatory_terms_json = {"mode": "days", "price_unit": "day"}
                else:
                    listing.mandatory_price_stars = None
                    listing.mandatory_terms_json = None
        listing = await _get_listing(listing_id)
        if listing is not None:
            await _render_editor(callback, listing)
        await callback.answer("Типы рекламы обновлены")

    @router.callback_query(F.data.startswith("ads:edit:post_price:"))
    async def edit_post_price(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError:
            return
        listing = await _owned_listing(listing_id, callback.from_user.id)
        if listing is None or not listing.offers_post:
            await callback.answer("Посты в этом объявлении выключены.", show_alert=True)
            return
        await state.set_state(AdvertisingEditState.waiting_post_price)
        await state.update_data(edit_listing_id=listing_id, edit_prompt_message_id=callback.message.message_id)
        await callback.message.edit_text(
            "⭐ <b>Цена рекламного поста</b>\n\nВведите новую цену за сутки в Telegram Stars.",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:post_interval:"))
    async def edit_post_interval(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError:
            return
        listing = await _owned_listing(listing_id, callback.from_user.id)
        if listing is None or not listing.offers_post:
            await callback.answer("Посты в этом объявлении выключены.", show_alert=True)
            return
        await state.set_state(AdvertisingEditState.waiting_post_interval)
        await state.update_data(edit_listing_id=listing_id, edit_prompt_message_id=callback.message.message_id)
        await callback.message.edit_text(
            "⏱ <b>Интервал публикации</b>\n\nВведите новый интервал в часах целым положительным числом.",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:mandatory_mode:"))
    async def edit_mandatory_mode(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError:
            return
        listing = await _owned_listing(listing_id, callback.from_user.id)
        if listing is None or not listing.offers_mandatory:
            await callback.answer("ОП в этом объявлении выключена.", show_alert=True)
            return
        await callback.message.edit_text(
            "📐 <b>Способ расчёта ОП</b>\n\nВыберите новый вариант:",
            parse_mode="HTML",
            reply_markup=_mandatory_mode_keyboard(listing_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:set_mode:"))
    async def set_mandatory_mode(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 5:
            return
        try:
            listing_id = int(parts[3])
        except ValueError:
            return
        mode = parts[4]
        if mode not in {"days", "subscribers"}:
            return
        async with session_factory() as session:
            async with session.begin():
                listing = (
                    await session.execute(
                        select(AdvertisingListing)
                        .where(
                            AdvertisingListing.id == listing_id,
                            AdvertisingListing.owner_user_id == callback.from_user.id,
                            AdvertisingListing.offers_mandatory.is_(True),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                listing.mandatory_terms_json = {
                    "mode": mode,
                    "price_unit": "day" if mode == "days" else "subscriber",
                }
        listing = await _get_listing(listing_id)
        if listing is not None:
            await _render_editor(callback, listing)
        await callback.answer("Способ расчёта обновлён")

    @router.callback_query(F.data.startswith("ads:edit:mandatory_price:"))
    async def edit_mandatory_price(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError:
            return
        listing = await _owned_listing(listing_id, callback.from_user.id)
        if listing is None or not listing.offers_mandatory:
            await callback.answer("ОП в этом объявлении выключена.", show_alert=True)
            return
        terms = listing.mandatory_terms_json or {}
        unit = "за 1 день" if terms.get("mode") == "days" else "за 1 подписчика"
        await state.set_state(AdvertisingEditState.waiting_mandatory_price)
        await state.update_data(edit_listing_id=listing_id, edit_prompt_message_id=callback.message.message_id)
        await callback.message.edit_text(
            f"⭐ <b>Цена ОП</b>\n\nВведите новую цену {unit} в Telegram Stars.",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:edit:toggle:"))
    async def toggle_listing(callback: CallbackQuery) -> None:
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError:
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
                listing.is_active = not listing.is_active
        listing = await _get_listing(listing_id)
        if listing is not None:
            await _render_editor(callback, listing)
        await callback.answer("Статус объявления обновлён")

    async def _save_number(message: Message, state: FSMContext, field: str, *, multiplier: int = 1) -> None:
        if message.from_user is None:
            return
        try:
            value = int((message.text or "").strip())
        except ValueError:
            value = 0
        try:
            await message.delete()
        except Exception:
            pass
        data = await state.get_data()
        listing_id = data.get("edit_listing_id")
        prompt_id = data.get("edit_prompt_message_id")
        if value <= 0 or not isinstance(listing_id, int):
            if isinstance(prompt_id, int):
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=prompt_id,
                        text="Введите целое положительное число.",
                    )
                except Exception:
                    pass
            return
        async with session_factory() as session:
            async with session.begin():
                listing = (
                    await session.execute(
                        select(AdvertisingListing)
                        .where(
                            AdvertisingListing.id == listing_id,
                            AdvertisingListing.owner_user_id == message.from_user.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if listing is None:
                    await state.clear()
                    return
                setattr(listing, field, value * multiplier)
        await state.clear()
        listing = await _get_listing(listing_id)
        if listing is None:
            return
        if isinstance(prompt_id, int):
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                    text="✏️ <b>Редактирование объявления</b>\n\n" + _listing_text(listing),
                    parse_mode="HTML",
                    reply_markup=_editor_keyboard(listing),
                )
                return
            except Exception:
                pass
        await message.answer(
            "✏️ <b>Редактирование объявления</b>\n\n" + _listing_text(listing),
            parse_mode="HTML",
            reply_markup=_editor_keyboard(listing),
        )

    @router.message(AdvertisingEditState.waiting_post_price, F.chat.type == "private")
    async def save_post_price(message: Message, state: FSMContext) -> None:
        await _save_number(message, state, "post_price_stars")

    @router.message(AdvertisingEditState.waiting_post_interval, F.chat.type == "private")
    async def save_post_interval(message: Message, state: FSMContext) -> None:
        await _save_number(message, state, "post_interval_minutes", multiplier=60)

    @router.message(AdvertisingEditState.waiting_mandatory_price, F.chat.type == "private")
    async def save_mandatory_price(message: Message, state: FSMContext) -> None:
        await _save_number(message, state, "mandatory_price_stars")

    return router
