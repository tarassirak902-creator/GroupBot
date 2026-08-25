from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.routers.advertising import AdvertisingListingState
from groupbot.routers.advertising_post_duration import listing_text_with_duration


class AdvertisingCreationDurationState(StatesGroup):
    waiting_days = State()


def _mandatory_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 По дням", callback_data="ads:sell:mandatory_mode:days")],
        [InlineKeyboardButton(text="👥 По количеству подписчиков", callback_data="ads:sell:mandatory_mode:subscribers")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="ads:home")],
    ])


def _listing_created_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Открыть объявление", callback_data=f"ads:listing:{listing_id}")],
        [InlineKeyboardButton(text="📦 Мои продажи", callback_data="ads:my_sales")],
        [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
    ])


async def _edit_prompt(message: Message, state: FSMContext, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    data = await state.get_data()
    prompt_id = data.get("prompt_message_id")
    if isinstance(prompt_id, int):
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            pass
    sent = await message.answer(text, parse_mode="HTML", reply_markup=markup)
    await state.update_data(prompt_message_id=sent.message_id)


async def _save_listing(
    session_factory: async_sessionmaker[AsyncSession],
    state: FSMContext,
    user_id: int,
) -> AdvertisingListing | None:
    data = await state.get_data()
    chat_id = data.get("chat_id")
    if not isinstance(chat_id, int):
        return None

    async with session_factory() as session:
        async with session.begin():
            ownership = (
                await session.execute(
                    select(Group, GroupOwner)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(
                        Group.chat_id == chat_id,
                        GroupOwner.user_id == user_id,
                        GroupOwner.is_current.is_(True),
                        Group.status == GroupStatus.active.value,
                    )
                )
            ).first()
            if ownership is None:
                return None
            group, _owner = ownership

            listing = (
                await session.execute(
                    select(AdvertisingListing)
                    .where(AdvertisingListing.chat_id == chat_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if listing is None:
                listing = AdvertisingListing(
                    owner_user_id=user_id,
                    chat_id=chat_id,
                    group_title_snapshot=group.title or "Группа",
                    member_count_snapshot=int(data.get("member_count") or 0),
                    offers_post=bool(data.get("offers_post")),
                    offers_mandatory=bool(data.get("offers_mandatory")),
                )
                session.add(listing)
            else:
                listing.owner_user_id = user_id
                listing.group_title_snapshot = group.title or listing.group_title_snapshot
                listing.member_count_snapshot = int(data.get("member_count") or listing.member_count_snapshot)
                listing.offers_post = bool(data.get("offers_post"))
                listing.offers_mandatory = bool(data.get("offers_mandatory"))
                listing.is_active = True

            if listing.offers_post:
                listing.post_price_stars = int(data["post_price_stars"])
                listing.post_interval_minutes = int(data["post_interval_hours"]) * 60
                listing.post_terms_json = {
                    "price_period": "day",
                    "duration_days": int(data.get("post_duration_days") or 1),
                }
            else:
                listing.post_price_stars = None
                listing.post_interval_minutes = None
                listing.post_terms_json = None

            if listing.offers_mandatory:
                mode = str(data["mandatory_mode"])
                listing.mandatory_price_stars = int(data["mandatory_price_stars"])
                listing.mandatory_terms_json = {
                    "mode": mode,
                    "price_unit": "day" if mode == "days" else "subscriber",
                }
            else:
                listing.mandatory_price_stars = None
                listing.mandatory_terms_json = None

            await session.flush()
            listing_id = listing.id

        return (
            await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id))
        ).scalar_one()


def create_advertising_duration_integration_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_duration_integration")

    @router.message(AdvertisingListingState.waiting_post_interval, F.chat.type == "private")
    async def capture_post_interval(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        try:
            hours = int((message.text or "").strip())
        except ValueError:
            hours = 0
        try:
            await message.delete()
        except Exception:
            pass
        if hours <= 0:
            await _edit_prompt(
                message,
                state,
                "📣 <b>Рекламные посты</b>\n\nИнтервал должен быть целым положительным количеством часов. Попробуйте ещё раз.",
            )
            return

        await state.update_data(post_interval_hours=hours)
        await state.set_state(AdvertisingCreationDurationState.waiting_days)
        await _edit_prompt(
            message,
            state,
            "⏳ <b>Срок размещения рекламного поста</b>\n\n"
            f"⏱ Интервал публикации: <b>{hours} ч.</b>\n\n"
            "Укажите, сколько дней должен длиться показ рекламного поста.\n"
            "Введите целое число от <b>1 до 365</b>.",
        )

    @router.message(AdvertisingCreationDurationState.waiting_days, F.chat.type == "private")
    async def capture_duration(message: Message, state: FSMContext) -> None:
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
        if days < 1 or days > 365:
            await _edit_prompt(message, state, "⏳ Введите срок размещения от <b>1 до 365 дней</b>.")
            return

        await state.update_data(post_duration_days=days)
        data = await state.get_data()
        if data.get("offers_mandatory"):
            await state.set_state(None)
            await _edit_prompt(
                message,
                state,
                "✅ <b>Обязательная подписка</b>\n\nКак вы хотите рассчитывать ОП?",
                _mandatory_mode_keyboard(),
            )
            return

        listing = await _save_listing(session_factory, state, message.from_user.id)
        if listing is None:
            await state.clear()
            await message.answer("Не удалось сохранить объявление. Проверьте, что группа активна и принадлежит вам.")
            return
        prompt_id = (await state.get_data()).get("prompt_message_id")
        await state.clear()
        text = "✅ <b>Рекламное объявление опубликовано</b>\n\n" + listing_text_with_duration(listing)
        if isinstance(prompt_id, int):
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=_listing_created_keyboard(listing.id),
                )
                return
            except Exception:
                pass
        await message.answer(text, parse_mode="HTML", reply_markup=_listing_created_keyboard(listing.id))

    @router.message(AdvertisingListingState.waiting_mandatory_price, F.chat.type == "private")
    async def save_mandatory_price(message: Message, state: FSMContext) -> None:
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
        if value <= 0:
            await _edit_prompt(message, state, "✅ <b>Обязательная подписка</b>\n\nЦена должна быть целым положительным числом Stars. Попробуйте ещё раз.")
            return
        await state.update_data(mandatory_price_stars=value)
        listing = await _save_listing(session_factory, state, message.from_user.id)
        if listing is None:
            await state.clear()
            await message.answer("Не удалось сохранить объявление. Проверьте, что группа активна и принадлежит вам.")
            return
        prompt_id = (await state.get_data()).get("prompt_message_id")
        await state.clear()
        text = "✅ <b>Рекламное объявление опубликовано</b>\n\n" + listing_text_with_duration(listing)
        if isinstance(prompt_id, int):
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=_listing_created_keyboard(listing.id),
                )
                return
            except Exception:
                pass
        await message.answer(text, parse_mode="HTML", reply_markup=_listing_created_keyboard(listing.id))

    return router
