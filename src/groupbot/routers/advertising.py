from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing
from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, GroupStatus


class AdvertisingListingState(StatesGroup):
    waiting_post_price = State()
    waiting_post_interval = State()
    waiting_mandatory_price = State()


def _advertising_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛒 Купить рекламу", callback_data="ads:buy"),
                InlineKeyboardButton(text="💼 Продать рекламу", callback_data="ads:sell"),
            ],
            [
                InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys"),
                InlineKeyboardButton(text="📦 Мои продажи", callback_data="ads:my_sales"),
            ],
            [InlineKeyboardButton(text="⭐ Отзывы и споры", callback_data="ads:reviews")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _advertising_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _sell_groups_keyboard(groups: list[tuple[int, str | None]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, title in groups:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🏠 {title or 'Группа'}"[:64],
                    callback_data=f"ads:sell:group:{chat_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _offer_type_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Рекламные посты", callback_data=f"ads:sell:type:{chat_id}:post")],
            [InlineKeyboardButton(text="✅ Обязательная подписка", callback_data=f"ads:sell:type:{chat_id}:mandatory")],
            [InlineKeyboardButton(text="📣 + ✅ Посты и ОП", callback_data=f"ads:sell:type:{chat_id}:both")],
            [InlineKeyboardButton(text="◀️ Выбор группы", callback_data="ads:sell")],
        ]
    )


def _mandatory_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 По дням", callback_data="ads:sell:mandatory_mode:days")],
            [InlineKeyboardButton(text="👥 По количеству подписчиков", callback_data="ads:sell:mandatory_mode:subscribers")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="ads:home")],
        ]
    )


def _listing_created_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Открыть объявление", callback_data=f"ads:listing:{listing_id}")],
            [InlineKeyboardButton(text="📦 Мои продажи", callback_data="ads:my_sales")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]
    )


def _listings_keyboard(rows: list[AdvertisingListing], *, own: bool) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for listing in rows:
        kinds: list[str] = []
        if listing.offers_post:
            kinds.append("📣")
        if listing.offers_mandatory:
            kinds.append("✅")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{''.join(kinds)} {listing.group_title_snapshot}"[:64],
                    callback_data=f"ads:listing:{listing.id}",
                )
            ]
        )
    if own:
        buttons.append([InlineKeyboardButton(text="➕ Создать/изменить объявление", callback_data="ads:sell")])
    buttons.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _listing_text(listing: AdvertisingListing) -> str:
    members = f"{listing.member_count_snapshot:,}".replace(",", " ")
    lines = [
        "📢 <b>Рекламное объявление</b>",
        "",
        f"🏠 Группа: <b>{listing.group_title_snapshot}</b>",
        f"👥 Участников: <b>{members}</b>",
        "",
    ]
    if listing.offers_post:
        interval = listing.post_interval_minutes or 0
        hours = interval / 60 if interval else 0
        hours_text = str(int(hours)) if hours.is_integer() else str(hours)
        lines.extend(
            [
                "📣 <b>Рекламные посты</b>",
                f"⭐ Цена за сутки: <b>{listing.post_price_stars} ⭐</b>",
                f"⏱ Интервал публикации: <b>{hours_text} ч.</b>",
                "",
            ]
        )
    if listing.offers_mandatory:
        terms = listing.mandatory_terms_json or {}
        mode = terms.get("mode")
        unit = "за 1 день" if mode == "days" else "за 1 подписчика"
        lines.extend(
            [
                "✅ <b>Обязательная подписка</b>",
                f"📐 Расчёт: <b>{'по дням' if mode == 'days' else 'по количеству подписчиков'}</b>",
                f"⭐ Цена {unit}: <b>{listing.mandatory_price_stars} ⭐</b>",
            ]
        )
    return "\n".join(lines)


def _creator_advertising_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Рекламная площадка", callback_data="creator:ads:marketplace")],
            [InlineKeyboardButton(text="🔔 Обязательные подписки", callback_data="creator:ads:mandatory")],
            [InlineKeyboardButton(text="⭐ Отзывы и споры", callback_data="creator:ads:reviews")],
            [InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _creator_advertising_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Creator-реклама", callback_data="creator:ads:home")],
            [InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")],
        ]
    )


def create_advertising_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    router = Router(name="advertising")

    async def _owned_active_groups(user_id: int) -> list[tuple[int, str | None]]:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Group.chat_id, Group.title)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(
                        GroupOwner.user_id == user_id,
                        GroupOwner.is_current.is_(True),
                        Group.status == GroupStatus.active.value,
                    )
                    .order_by(Group.title, Group.chat_id)
                )
            ).all()
        return list(rows)

    async def _save_listing(state: FSMContext, user_id: int) -> AdvertisingListing | None:
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
                    listing.post_terms_json = {"price_period": "day"}
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

    async def _edit_prompt(message: Message, state: FSMContext, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        data = await state.get_data()
        prompt_message_id = data.get("prompt_message_id")
        if isinstance(prompt_message_id, int):
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                return
            except Exception:
                pass
        sent = await message.answer(text, parse_mode="HTML", reply_markup=markup)
        await state.update_data(prompt_message_id=sent.message_id)

    @router.message(F.chat.type == "private", F.text == "📢 Реклама")
    async def advertising_home_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🟣 <b>Mimorus · Реклама</b>\n\n"
            "Покупайте рекламные размещения или выставляйте свою подключённую группу как площадку.",
            parse_mode="HTML",
            reply_markup=_advertising_keyboard(),
        )

    @router.callback_query(F.data == "ads:home")
    async def advertising_home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text(
                "🟣 <b>Mimorus · Реклама</b>\n\n"
                "Покупайте рекламные размещения или выставляйте свою подключённую группу как площадку.",
                parse_mode="HTML",
                reply_markup=_advertising_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:sell")
    async def sell_advertising(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        groups = await _owned_active_groups(callback.from_user.id)
        if callback.message is None:
            return
        if not groups:
            await callback.message.edit_text(
                "💼 <b>Продать рекламу</b>\n\n"
                "Для создания объявления нужна хотя бы одна активная подключённая группа, которой вы владеете.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
            await callback.answer()
            return
        await callback.message.edit_text(
            "💼 <b>Продать рекламу</b>\n\nВыберите группу, для которой хотите создать рекламное объявление:",
            parse_mode="HTML",
            reply_markup=_sell_groups_keyboard(groups),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:sell:group:"))
    async def choose_sell_group(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            chat_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        groups = dict(await _owned_active_groups(callback.from_user.id))
        if chat_id not in groups:
            await callback.answer("Эта группа вам не принадлежит или не активна.", show_alert=True)
            return
        try:
            member_count = await bot.get_chat_member_count(chat_id)
        except Exception:
            member_count = 0
        await state.clear()
        await state.update_data(
            chat_id=chat_id,
            group_title=groups[chat_id] or "Группа",
            member_count=member_count,
            prompt_message_id=callback.message.message_id,
        )
        await callback.message.edit_text(
            "💼 <b>Продать рекламу</b>\n\n"
            f"🏠 Группа: <b>{groups[chat_id] or 'Группа'}</b>\n"
            f"👥 Участников: <b>{member_count:,}</b>\n\n".replace(",", " ")
            + "Что вы хотите продавать?",
            parse_mode="HTML",
            reply_markup=_offer_type_keyboard(chat_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:sell:type:"))
    async def choose_offer_type(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 5:
            await callback.answer("Некорректный формат.", show_alert=True)
            return
        try:
            chat_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        kind = parts[4]
        data = await state.get_data()
        if data.get("chat_id") != chat_id or kind not in {"post", "mandatory", "both"}:
            await callback.answer("Мастер создания объявления устарел. Начните заново.", show_alert=True)
            return
        offers_post = kind in {"post", "both"}
        offers_mandatory = kind in {"mandatory", "both"}
        await state.update_data(offers_post=offers_post, offers_mandatory=offers_mandatory)
        if offers_post:
            await state.set_state(AdvertisingListingState.waiting_post_price)
            await callback.message.edit_text(
                "📣 <b>Рекламные посты</b>\n\n"
                "Введите стоимость размещения рекламного поста <b>за сутки</b> в Telegram Stars.\n\n"
                "Отправьте только целое положительное число, например: <code>50</code>.",
                parse_mode="HTML",
            )
        else:
            await callback.message.edit_text(
                "✅ <b>Обязательная подписка</b>\n\nКак вы хотите рассчитывать ОП?",
                parse_mode="HTML",
                reply_markup=_mandatory_mode_keyboard(),
            )
        await callback.answer()

    @router.message(AdvertisingListingState.waiting_post_price, F.chat.type == "private")
    async def post_price(message: Message, state: FSMContext) -> None:
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
            await _edit_prompt(
                message,
                state,
                "📣 <b>Рекламные посты</b>\n\nЦена должна быть целым положительным числом Stars. Попробуйте ещё раз.",
            )
            return
        await state.update_data(post_price_stars=value)
        await state.set_state(AdvertisingListingState.waiting_post_interval)
        await _edit_prompt(
            message,
            state,
            "📣 <b>Рекламные посты</b>\n\n"
            f"⭐ Цена за сутки: <b>{value} ⭐</b>\n\n"
            "Теперь укажите интервал публикации поста <b>в часах</b>.\n"
            "Отправьте целое положительное число.",
        )

    @router.message(AdvertisingListingState.waiting_post_interval, F.chat.type == "private")
    async def post_interval(message: Message, state: FSMContext) -> None:
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
            await _edit_prompt(
                message,
                state,
                "📣 <b>Рекламные посты</b>\n\nИнтервал должен быть целым положительным количеством часов. Попробуйте ещё раз.",
            )
            return
        await state.update_data(post_interval_hours=value)
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
        listing = await _save_listing(state, message.from_user.id)
        if listing is None:
            await state.clear()
            await message.answer("Не удалось сохранить объявление. Проверьте, что группа активна и принадлежит вам.")
            return
        await state.clear()
        await _edit_prompt(
            message,
            state,
            "✅ <b>Рекламное объявление опубликовано</b>\n\n" + _listing_text(listing),
            _listing_created_keyboard(listing.id),
        )

    @router.callback_query(F.data.startswith("ads:sell:mandatory_mode:"))
    async def mandatory_mode(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        mode = (callback.data or "").rsplit(":", 1)[-1]
        if mode not in {"days", "subscribers"}:
            await callback.answer("Некорректный способ расчёта.", show_alert=True)
            return
        data = await state.get_data()
        if not data.get("offers_mandatory"):
            await callback.answer("Мастер создания объявления устарел. Начните заново.", show_alert=True)
            return
        await state.update_data(mandatory_mode=mode)
        await state.set_state(AdvertisingListingState.waiting_mandatory_price)
        unit = "за 1 день" if mode == "days" else "за 1 подписчика"
        await callback.message.edit_text(
            "✅ <b>Обязательная подписка</b>\n\n"
            f"Введите стоимость <b>{unit}</b> в Telegram Stars.\n\n"
            "Отправьте только целое положительное число.",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(AdvertisingListingState.waiting_mandatory_price, F.chat.type == "private")
    async def mandatory_price(message: Message, state: FSMContext) -> None:
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
            await _edit_prompt(
                message,
                state,
                "✅ <b>Обязательная подписка</b>\n\nЦена должна быть целым положительным числом Stars. Попробуйте ещё раз.",
            )
            return
        await state.update_data(mandatory_price_stars=value)
        listing = await _save_listing(state, message.from_user.id)
        if listing is None:
            await state.clear()
            await message.answer("Не удалось сохранить объявление. Проверьте, что группа активна и принадлежит вам.")
            return
        prompt_message_id = (await state.get_data()).get("prompt_message_id")
        await state.clear()
        if isinstance(prompt_message_id, int):
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text="✅ <b>Рекламное объявление опубликовано</b>\n\n" + _listing_text(listing),
                    parse_mode="HTML",
                    reply_markup=_listing_created_keyboard(listing.id),
                )
                return
            except Exception:
                pass
        await message.answer(
            "✅ <b>Рекламное объявление опубликовано</b>\n\n" + _listing_text(listing),
            parse_mode="HTML",
            reply_markup=_listing_created_keyboard(listing.id),
        )

    @router.callback_query(F.data == "ads:buy")
    async def buy_advertising(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AdvertisingListing)
                    .where(
                        AdvertisingListing.is_active.is_(True),
                        AdvertisingListing.owner_user_id != callback.from_user.id,
                    )
                    .order_by(AdvertisingListing.updated_at.desc(), AdvertisingListing.id.desc())
                    .limit(50)
                )
            ).scalars().all()
        if not rows:
            await callback.message.edit_text(
                "🛒 <b>Купить рекламу</b>\n\nАктивных объявлений других рекламодателей пока нет.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
        else:
            await callback.message.edit_text(
                "🛒 <b>Купить рекламу</b>\n\nВыберите рекламную площадку:",
                parse_mode="HTML",
                reply_markup=_listings_keyboard(list(rows), own=False),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:my_sales")
    async def my_sales(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AdvertisingListing)
                    .where(AdvertisingListing.owner_user_id == callback.from_user.id)
                    .order_by(AdvertisingListing.updated_at.desc(), AdvertisingListing.id.desc())
                )
            ).scalars().all()
        text = "📦 <b>Мои продажи</b>\n\nВыберите объявление:" if rows else "📦 <b>Мои продажи</b>\n\nУ вас пока нет рекламных объявлений."
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_listings_keyboard(list(rows), own=True),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:listing:"))
    async def open_listing(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        async with session_factory() as session:
            listing = (
                await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id))
            ).scalar_one_or_none()
        if listing is None or (not listing.is_active and listing.owner_user_id != callback.from_user.id):
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        own = listing.owner_user_id == callback.from_user.id
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if own:
            keyboard_rows.append([InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ads:sell:group:{listing.chat_id}")])
            keyboard_rows.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
        else:
            keyboard_rows.append([InlineKeyboardButton(text="📨 Отправить запрос", callback_data=f"ads:request:{listing.id}")])
            keyboard_rows.append([InlineKeyboardButton(text="◀️ Купить рекламу", callback_data="ads:buy")])
        keyboard_rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
        await callback.message.edit_text(
            _listing_text(listing),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:request:"))
    async def request_placeholder(callback: CallbackQuery) -> None:
        await callback.answer("Отправку заявки подключим следующим блоком.", show_alert=True)

    @router.callback_query(F.data == "ads:my_buys")
    async def my_buys(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "📋 <b>Мои покупки</b>\n\nЗаявки и активные рекламные сделки появятся здесь после отправки первого запроса.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:reviews")
    async def advertising_reviews(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "⭐ <b>Отзывы и споры</b>\n\n"
                "Отзывы будут доступны после завершённых рекламных сделок. Спор можно будет открыть во время активной рекламы и до окончательного закрытия сделки.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.in_({"creator:section:ads", "creator:ads:home"}))
    async def creator_advertising_home(callback: CallbackQuery) -> None:
        if callback.from_user.id not in settings.creator_id_set:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(
                "📢 <b>Creator-реклама</b>\n\nУправление рекламной частью Mimorus:",
                parse_mode="HTML",
                reply_markup=_creator_advertising_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:ads:"))
    async def creator_advertising_section(callback: CallbackQuery) -> None:
        if callback.from_user.id not in settings.creator_id_set:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        section = (callback.data or "").rsplit(":", 1)[-1]
        labels = {
            "marketplace": "📣 Рекламная площадка",
            "mandatory": "🔔 Обязательные подписки",
            "reviews": "⭐ Отзывы и споры",
        }
        label = labels.get(section)
        if label is None:
            return
        if callback.message is not None:
            await callback.message.edit_text(
                f"{label}\n\nCreator-инструменты этого блока будут подключены к общей рекламной модели без отдельного параллельного маркетплейса.",
                reply_markup=_creator_advertising_back_keyboard(),
            )
        await callback.answer()

    return router
