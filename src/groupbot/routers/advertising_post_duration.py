from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing


class AdvertisingPostDurationState(StatesGroup):
    waiting_days = State()


def post_duration_days(listing: AdvertisingListing) -> int:
    terms = listing.post_terms_json or {}
    try:
        return max(int(terms.get("duration_days") or 1), 1)
    except (TypeError, ValueError):
        return 1


def listing_text_with_duration(listing: AdvertisingListing) -> str:
    members = f"{listing.member_count_snapshot:,}".replace(",", " ")
    lines = [
        "📢 <b>Рекламное объявление</b>", "",
        f"🏠 Группа: <b>{listing.group_title_snapshot}</b>",
        f"👥 Участников: <b>{members}</b>",
        f"📌 Статус: <b>{'активно' if listing.is_active else 'выключено'}</b>", "",
    ]
    if listing.offers_post:
        interval = listing.post_interval_minutes or 0
        hours = interval / 60 if interval else 0
        hours_text = str(int(hours)) if float(hours).is_integer() else str(hours)
        days = post_duration_days(listing)
        price = int(listing.post_price_stars or 0)
        lines += [
            "📣 <b>Рекламные посты</b>",
            f"⭐ Цена за сутки: <b>{price} ⭐</b>",
            f"⏳ Срок размещения: <b>{days} дн.</b>",
            f"💰 Стоимость размещения: <b>{price * days} ⭐</b>",
            f"⏱ Интервал публикации: <b>{hours_text} ч.</b>", "",
        ]
    if listing.offers_mandatory:
        terms = listing.mandatory_terms_json or {}
        mode = terms.get("mode")
        unit = "за 1 день" if mode == "days" else "за 1 подписчика"
        lines += [
            "✅ <b>Обязательная подписка</b>",
            f"📐 Расчёт: <b>{'по дням' if mode == 'days' else 'по количеству подписчиков'}</b>",
            f"⭐ Цена {unit}: <b>{listing.mandatory_price_stars} ⭐</b>",
        ]
    return "\n".join(lines)


def editor_keyboard_with_duration(listing: AdvertisingListing) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🎛 Типы рекламы", callback_data=f"ads:edit:types:{listing.id}")]]
    if listing.offers_post:
        rows += [
            [InlineKeyboardButton(text="⭐ Цена поста", callback_data=f"ads:edit:post_price:{listing.id}")],
            [InlineKeyboardButton(text="⏳ Срок размещения", callback_data=f"ads:edit:post_duration:{listing.id}")],
            [InlineKeyboardButton(text="⏱ Интервал поста", callback_data=f"ads:edit:post_interval:{listing.id}")],
        ]
    if listing.offers_mandatory:
        rows += [
            [InlineKeyboardButton(text="📐 Способ расчёта ОП", callback_data=f"ads:edit:mandatory_mode:{listing.id}")],
            [InlineKeyboardButton(text="⭐ Цена ОП", callback_data=f"ads:edit:mandatory_price:{listing.id}")],
        ]
    rows.append([InlineKeyboardButton(text="⛔ Выключить объявление" if listing.is_active else "✅ Включить объявление", callback_data=f"ads:edit:toggle:{listing.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К объявлению", callback_data=f"ads:listing:{listing.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def post_editor_keyboard_with_cancel(deal_id: int, *, has_photo: bool, has_button: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"ads:post:text:{deal_id}"), InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"ads:post:photo:{deal_id}")],
        [InlineKeyboardButton(text="🔘 Изменить кнопку", callback_data=f"ads:post:button:{deal_id}")],
    ]
    if has_photo or has_button:
        extra = []
        if has_photo:
            extra.append(InlineKeyboardButton(text="🗑 Фото", callback_data=f"ads:post:remove_photo:{deal_id}"))
        if has_button:
            extra.append(InlineKeyboardButton(text="🗑 Кнопку", callback_data=f"ads:post:remove_button:{deal_id}"))
        rows.append(extra)
    rows.append([InlineKeyboardButton(text="✅ Подтвердить и продолжить", callback_data=f"ads:post:submit2:{deal_id}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить рекламный пост", callback_data=f"ads:post:cancel:{deal_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_advertising_post_duration_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_post_duration")

    from groupbot.routers.advertising_request_guard import create_advertising_request_guard_router
    router.include_router(create_advertising_request_guard_router(session_factory))

    @router.callback_query(F.data.regexp(r"^ads:listing:\d+$"))
    async def open_listing_with_contact(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        listing_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id))).scalar_one_or_none()
        if listing is None or (not listing.is_active and listing.owner_user_id != callback.from_user.id):
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        if listing.owner_user_id == callback.from_user.id:
            rows = [
                [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"ads:edit:{listing.id}")],
                [InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]
        else:
            rows = [
                [InlineKeyboardButton(text="📨 Отправить запрос", callback_data=f"ads:request:{listing.id}")],
                [InlineKeyboardButton(text="💬 Связаться с рекламодателем", url=f"tg://user?id={listing.owner_user_id}")],
                [InlineKeyboardButton(text="◀️ Вернуться к списку", callback_data="ads:buy")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]
        await callback.message.edit_text(listing_text_with_duration(listing), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:edit:post_duration:\d+$"))
    async def start_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        listing_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id, AdvertisingListing.owner_user_id == callback.from_user.id, AdvertisingListing.offers_post.is_(True)))).scalar_one_or_none()
        if listing is None:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        await state.set_state(AdvertisingPostDurationState.waiting_days)
        await state.update_data(post_duration_listing_id=listing_id, post_duration_prompt_id=callback.message.message_id)
        await callback.message.edit_text("⏳ <b>Срок размещения рекламного поста</b>\n\nВведите количество дней от 1 до 365.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data=f"ads:edit:{listing_id}")]]))
        await callback.answer()

    @router.message(AdvertisingPostDurationState.waiting_days, F.chat.type == "private")
    async def save_duration(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        try:
            days = int((message.text or "").strip())
        except ValueError:
            days = 0
        try:
            await message.delete()
        except Exception:
            pass
        data = await state.get_data()
        listing_id = data.get("post_duration_listing_id")
        prompt_id = data.get("post_duration_prompt_id")
        if not isinstance(listing_id, int) or days < 1 or days > 365:
            if isinstance(prompt_id, int):
                try:
                    await message.bot.edit_message_text(chat_id=message.chat.id, message_id=prompt_id, text="⏳ Введите срок от 1 до 365 дней.")
                except Exception:
                    pass
            return
        async with session_factory() as session:
            async with session.begin():
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id, AdvertisingListing.owner_user_id == message.from_user.id).with_for_update())).scalar_one_or_none()
                if listing is None:
                    await state.clear()
                    return
                terms = dict(listing.post_terms_json or {})
                terms.update({"price_period": "day", "duration_days": days})
                listing.post_terms_json = terms
        await state.clear()
        async with session_factory() as session:
            listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id))).scalar_one()
        text = "✏️ <b>Редактирование объявления</b>\n\n" + listing_text_with_duration(listing)
        if isinstance(prompt_id, int):
            try:
                await message.bot.edit_message_text(chat_id=message.chat.id, message_id=prompt_id, text=text, parse_mode="HTML", reply_markup=editor_keyboard_with_duration(listing))
                return
            except Exception:
                pass
        await message.answer(text, parse_mode="HTML", reply_markup=editor_keyboard_with_duration(listing))

    from groupbot.routers.advertising_duration_integration import create_advertising_duration_integration_router
    router.include_router(create_advertising_duration_integration_router(session_factory))
    return router
