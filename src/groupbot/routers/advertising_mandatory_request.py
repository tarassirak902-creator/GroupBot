from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.models import Group, GroupOwner, GroupStatus

MANDATORY_DAILY_LIMIT = 3
POST_DAILY_LIMIT = 1


class MandatoryRequestState(StatesGroup):
    waiting_target = State()
    waiting_quantity = State()


def _target_ref(text: str) -> str | None:
    value = text.strip()
    if value.startswith("https://t.me/"):
        value = value.split("https://t.me/", 1)[1].split("?", 1)[0].strip("/")
    elif value.startswith("http://t.me/"):
        value = value.split("http://t.me/", 1)[1].split("?", 1)[0].strip("/")
    value = value.removeprefix("@").strip()
    if not value or "/" in value or "+" in value:
        return None
    return "@" + value


def _duration_days(deal: AdvertisingDeal) -> int:
    terms = (deal.agreed_terms_json or {}).get("post_terms") or {}
    try:
        return max(int(terms.get("duration_days") or 1), 1)
    except (TypeError, ValueError):
        return 1


def _mandatory_mode(listing: AdvertisingListing) -> str:
    mode = str((listing.mandatory_terms_json or {}).get("mode") or "days")
    return mode if mode in {"days", "subscribers"} else "days"


def _seller_keyboard(deal_id: int, buyer_id: int, *, has_post: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_post:
        rows.append([InlineKeyboardButton(text="👁 Посмотреть пост", callback_data=f"ads:post:view:{deal_id}")])
    rows.append([InlineKeyboardButton(text="💬 Связаться с покупателем", url=f"tg://user?id={buyer_id}")])
    rows.append([
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ads:mandatory:accept:{deal_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:mandatory:reject:{deal_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _post_only_seller_keyboard(deal_id: int, buyer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Посмотреть пост", callback_data=f"ads:post:view:{deal_id}")],
        [InlineKeyboardButton(text="💬 Связаться с покупателем", url=f"tg://user?id={buyer_id}")],
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ads:deal:accept:{deal_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:deal:reject:{deal_id}"),
        ],
    ])


async def _set_target_state(state: FSMContext, *, listing_id: int | None = None, deal_id: int | None = None) -> None:
    await state.set_state(MandatoryRequestState.waiting_target)
    payload: dict[str, int] = {}
    if listing_id is not None:
        payload["mandatory_listing_id"] = listing_id
    if deal_id is not None:
        payload["mandatory_existing_deal_id"] = deal_id
    await state.update_data(**payload)


async def _owned_active_groups(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> list[tuple[int, str]]:
    async with session_factory() as session:
        rows = (await session.execute(
            select(Group.chat_id, Group.title)
            .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
            .where(
                GroupOwner.user_id == user_id,
                GroupOwner.is_current.is_(True),
                Group.status == GroupStatus.active.value,
            )
            .order_by(Group.title, Group.chat_id)
        )).all()
    return [(int(chat_id), title or "Группа") for chat_id, title in rows]


def _target_picker_keyboard(groups: list[tuple[int, str]], *, cancel_callback: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, title in groups[:25]:
        short = title.strip()
        if len(short) > 45:
            short = short[:44].rstrip() + "…"
        rows.append([
            InlineKeyboardButton(text=f"🏠 {short}", callback_data=f"ads:mandatory:own:{chat_id}")
        ])
    rows.append([InlineKeyboardButton(text="✍️ Указать другую группу вручную", callback_data="ads:mandatory:manual")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _target_picker_text(*, after_post: bool = False) -> str:
    prefix = "✅ <b>Рекламный пост готов</b>\n\n" if after_post else "✅ <b>Покупка обязательной подписки</b>\n\n"
    return (
        prefix
        + "Выберите свою группу/канал, куда должна идти обязательная подписка.\n\n"
        + "Ниже показаны ваши активные группы, подключённые к Mimorus. "
        + "Также можно указать другую площадку вручную."
    )


def create_advertising_mandatory_request_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_mandatory_request")

    async def _show_target_picker(
        callback: CallbackQuery,
        state: FSMContext,
        *,
        listing_id: int | None = None,
        deal_id: int | None = None,
        after_post: bool = False,
    ) -> None:
        await state.clear()
        await _set_target_state(state, listing_id=listing_id, deal_id=deal_id)
        groups = await _owned_active_groups(session_factory, callback.from_user.id)
        cancel_callback = f"ads:mandatory:cancel:{deal_id}" if deal_id is not None else "ads:buy"
        if callback.message is not None:
            await callback.message.edit_text(
                _target_picker_text(after_post=after_post),
                parse_mode="HTML",
                reply_markup=_target_picker_keyboard(groups, cancel_callback=cancel_callback),
            )

    async def _context_listing(state: FSMContext, user_id: int) -> tuple[AdvertisingListing | None, int | None]:
        data = await state.get_data()
        listing_id = data.get("mandatory_listing_id")
        existing_deal_id = data.get("mandatory_existing_deal_id")
        async with session_factory() as session:
            if isinstance(existing_deal_id, int):
                row = (await session.execute(
                    select(AdvertisingDeal, AdvertisingListing)
                    .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                    .where(
                        AdvertisingDeal.id == existing_deal_id,
                        AdvertisingDeal.buyer_user_id == user_id,
                        AdvertisingDeal.status == "draft_mandatory",
                        AdvertisingDeal.requested_mandatory.is_(True),
                    )
                )).first()
                return (row[1], existing_deal_id) if row is not None else (None, existing_deal_id)
            if isinstance(listing_id, int):
                listing = (await session.execute(select(AdvertisingListing).where(
                    AdvertisingListing.id == listing_id,
                    AdvertisingListing.is_active.is_(True),
                    AdvertisingListing.offers_mandatory.is_(True),
                ))).scalar_one_or_none()
                if listing is not None and listing.owner_user_id == user_id:
                    return None, None
                return listing, None
        return None, None

    async def _prepare_quantity(
        *,
        state: FSMContext,
        user_id: int,
        target_chat_id: int,
        target_title: str,
        target_username: str,
        target_url: str,
        member_count: int,
    ) -> tuple[str | None, str | None]:
        listing, existing_deal_id = await _context_listing(state, user_id)
        if listing is None:
            await state.clear()
            return None, "Черновик заявки или объявление больше недоступны."
        mode = _mandatory_mode(listing)
        price = int(listing.mandatory_price_stars or 0)
        await state.set_state(MandatoryRequestState.waiting_quantity)
        await state.update_data(
            mandatory_listing_id=listing.id,
            mandatory_existing_deal_id=existing_deal_id,
            mandatory_target_chat_id=target_chat_id,
            mandatory_target_title=target_title,
            mandatory_target_username=target_username,
            mandatory_target_url=target_url,
            mandatory_target_member_count=member_count,
            mandatory_mode=mode,
            mandatory_price_stars=price,
        )
        title_safe = escape(target_title)
        if mode == "days":
            prompt = (
                "✅ <b>Группа для ОП проверена</b>\n\n"
                f"🏠 <a href=\"{target_url}\">{title_safe}</a>\n"
                f"👥 Участников: <b>{member_count:,}</b>\n\n".replace(",", " ")
                + f"⭐ Цена рекламодателя: <b>{price} ⭐ за день</b>\n\n"
                + "Введите количество дней ОП, например: <code>3</code>."
            )
        else:
            prompt = (
                "✅ <b>Группа для ОП проверена</b>\n\n"
                f"🏠 <a href=\"{target_url}\">{title_safe}</a>\n"
                f"👥 Участников: <b>{member_count:,}</b>\n\n".replace(",", " ")
                + f"⭐ Цена рекламодателя: <b>{price} ⭐ за подписчика</b>\n\n"
                + "Введите количество новых подписчиков, которое хотите получить, например: <code>100</code>."
            )
        cancel_callback = f"ads:mandatory:cancel:{existing_deal_id}" if existing_deal_id is not None else "ads:buy"
        return prompt, cancel_callback

    @router.callback_query(F.data.regexp(r"^ads:req:type:\d+:mandatory$"))
    async def start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        listing_id = int((callback.data or "").split(":")[3])
        async with session_factory() as session:
            listing = (await session.execute(select(AdvertisingListing).where(
                AdvertisingListing.id == listing_id,
                AdvertisingListing.is_active.is_(True),
                AdvertisingListing.offers_mandatory.is_(True),
            ))).scalar_one_or_none()
        if listing is None or listing.owner_user_id == callback.from_user.id:
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        await _show_target_picker(callback, state, listing_id=listing_id)
        await callback.answer()

    @router.callback_query(F.data == "ads:mandatory:manual")
    async def manual_target(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        current = await state.get_state()
        if current != MandatoryRequestState.waiting_target.state:
            await callback.answer("Мастер заявки устарел. Начните заново.", show_alert=True)
            return
        await callback.message.edit_text(
            "✅ <b>Укажите группу вручную</b>\n\n"
            "Отправьте публичный <b>@username</b> или ссылку <b>https://t.me/...</b>.\n\n"
            "⚠️ Mimorus должен быть администратором в этой группе/канале, чтобы проверять подписку.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Выбрать из моих групп", callback_data="ads:mandatory:picker")]
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data == "ads:mandatory:picker")
    async def reopen_picker(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        data = await state.get_data()
        listing_id = data.get("mandatory_listing_id")
        deal_id = data.get("mandatory_existing_deal_id")
        groups = await _owned_active_groups(session_factory, callback.from_user.id)
        cancel_callback = f"ads:mandatory:cancel:{deal_id}" if isinstance(deal_id, int) else "ads:buy"
        await callback.message.edit_text(
            _target_picker_text(after_post=isinstance(deal_id, int)),
            parse_mode="HTML",
            reply_markup=_target_picker_keyboard(groups, cancel_callback=cancel_callback),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:mandatory:own:-?\d+$"))
    async def select_own_target(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.message is None:
            return
        try:
            chat_id = int((callback.data or "").rsplit(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        async with session_factory() as session:
            owned = (await session.execute(
                select(Group.chat_id).join(GroupOwner, GroupOwner.chat_id == Group.chat_id).where(
                    Group.chat_id == chat_id,
                    Group.status == GroupStatus.active.value,
                    GroupOwner.user_id == callback.from_user.id,
                    GroupOwner.is_current.is_(True),
                )
            )).scalar_one_or_none()
        if owned is None:
            await callback.answer("Эта группа больше не подключена к вашему аккаунту.", show_alert=True)
            return
        try:
            target_chat = await bot.get_chat(chat_id)
            bot_info = await bot.get_me()
            bot_member = await bot.get_chat_member(chat_id, bot_info.id)
            if bot_member.status not in {"administrator", "creator"}:
                raise RuntimeError("bot is not admin")
            member_count = await bot.get_chat_member_count(chat_id)
        except Exception:
            await callback.answer("Mimorus не может проверить эту группу или больше не является её администратором.", show_alert=True)
            return
        username = str(getattr(target_chat, "username", None) or "")
        invite_link = str(getattr(target_chat, "invite_link", None) or "")
        target_url = f"https://t.me/{username}" if username else invite_link
        if not target_url:
            await callback.answer(
                "Для этой закрытой группы нет доступной ссылки входа. Добавьте публичный @username или доступную invite-ссылку и попробуйте снова.",
                show_alert=True,
            )
            return
        title = target_chat.title or (f"@{username}" if username else "Группа")
        prompt, cancel_callback = await _prepare_quantity(
            state=state,
            user_id=callback.from_user.id,
            target_chat_id=chat_id,
            target_title=title,
            target_username=username,
            target_url=target_url,
            member_count=member_count,
        )
        if prompt is None or cancel_callback is None:
            await callback.answer("Заявка больше недоступна.", show_alert=True)
            return
        await callback.message.edit_text(
            prompt,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=cancel_callback)]
            ]),
        )
        await callback.answer("Группа выбрана")

    @router.callback_query(F.data.regexp(r"^ads:post:submit2:\d+$"))
    async def submit_post_or_continue_op(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.message is None:
            return
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        seller_id: int | None = None
        title = ""
        requested_mandatory = False
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.buyer_user_id != callback.from_user.id or deal.status != "draft_post":
                    await callback.answer("Черновик недоступен.", show_alert=True)
                    return
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id))).scalar_one_or_none()
                post = (await session.execute(select(AdvertisingPlacement).where(
                    AdvertisingPlacement.deal_id == deal.id,
                    AdvertisingPlacement.kind == "post",
                ).with_for_update())).scalar_one_or_none()
                if listing is None or post is None:
                    await callback.answer("Не удалось подготовить заявку.", show_alert=True)
                    return
                cfg = dict(post.config_json or {})
                if not str(cfg.get("text") or "").strip() and not cfg.get("photo_file_id"):
                    await callback.answer("Пост должен содержать текст или фото.", show_alert=True)
                    return
                post.status = "ready"
                requested_mandatory = bool(deal.requested_mandatory)
                seller_id = deal.seller_user_id
                title = listing.group_title_snapshot
                deal.status = "draft_mandatory" if requested_mandatory else "pending"

        await state.clear()
        if requested_mandatory:
            groups = await _owned_active_groups(session_factory, callback.from_user.id)
            await _set_target_state(state, deal_id=deal_id)
            await callback.message.answer(
                _target_picker_text(after_post=True),
                parse_mode="HTML",
                reply_markup=_target_picker_keyboard(groups, cancel_callback=f"ads:mandatory:cancel:{deal_id}"),
            )
            await callback.answer("Теперь выберите площадку для ОП")
            return

        await callback.message.answer(
            "✅ <b>Рекламный пост отправлен рекламодателю на рассмотрение</b>\n\n"
            f"🏠 Площадка: <b>{escape(title)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")]]),
        )
        if seller_id is not None:
            try:
                await bot.send_message(
                    seller_id,
                    "📥 <b>Новая заявка на рекламный пост</b>\n\n"
                    f"🏠 Площадка: <b>{escape(title)}</b>\n"
                    "Покупатель уже подготовил пост. Посмотрите его и примите решение.",
                    parse_mode="HTML",
                    reply_markup=_post_only_seller_keyboard(deal_id, callback.from_user.id),
                )
            except Exception:
                pass
        await callback.answer("Отправлено рекламодателю")

    @router.message(MandatoryRequestState.waiting_target, F.chat.type == "private")
    async def target(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not message.text:
            return
        ref = _target_ref(message.text)
        try:
            await message.delete()
        except Exception:
            pass
        if ref is None:
            await message.answer("Укажите публичный @username или ссылку вида https://t.me/example.")
            return
        try:
            target_chat = await bot.get_chat(ref)
            bot_info = await bot.get_me()
            bot_member = await bot.get_chat_member(target_chat.id, bot_info.id)
            if bot_member.status not in {"administrator", "creator"}:
                raise RuntimeError("bot is not admin")
            member_count = await bot.get_chat_member_count(target_chat.id)
        except Exception:
            await message.answer("Не удалось проверить эту группу/канал. Убедитесь, что ссылка публичная и Mimorus добавлен туда администратором.")
            return
        username = str(getattr(target_chat, "username", None) or "")
        target_url = f"https://t.me/{username}" if username else str(getattr(target_chat, "invite_link", None) or "")
        if not target_url:
            await message.answer("Не удалось получить ссылку входа в эту группу/канал.")
            return
        prompt, cancel_callback = await _prepare_quantity(
            state=state,
            user_id=message.from_user.id,
            target_chat_id=target_chat.id,
            target_title=target_chat.title or (f"@{username}" if username else "Группа"),
            target_username=username,
            target_url=target_url,
            member_count=member_count,
        )
        if prompt is None or cancel_callback is None:
            await message.answer("Черновик заявки или объявление больше недоступны.")
            return
        await message.answer(
            prompt,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=cancel_callback)]
            ]),
        )

    @router.message(MandatoryRequestState.waiting_quantity, F.chat.type == "private")
    async def quantity(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        try:
            qty = int((message.text or "").strip())
        except ValueError:
            qty = 0
        try:
            await message.delete()
        except Exception:
            pass
        if qty <= 0 or qty > 1_000_000:
            await message.answer("Введите целое положительное число от 1 до 1 000 000.")
            return
        data = await state.get_data()
        listing_id = data.get("mandatory_listing_id")
        existing_deal_id = data.get("mandatory_existing_deal_id")
        target_chat_id = data.get("mandatory_target_chat_id")
        target_title = str(data.get("mandatory_target_title") or "Группа")
        target_username = str(data.get("mandatory_target_username") or "")
        target_url = str(data.get("mandatory_target_url") or "")
        member_count = int(data.get("mandatory_target_member_count") or 0)
        mode = str(data.get("mandatory_mode") or "days")
        price = int(data.get("mandatory_price_stars") or 0)
        if not isinstance(listing_id, int) or not isinstance(target_chat_id, int) or mode not in {"days", "subscribers"}:
            await state.clear()
            await message.answer("Мастер заявки устарел. Начните заново.")
            return
        total_price = price * qty

        async with session_factory() as session:
            async with session.begin():
                listing = (await session.execute(select(AdvertisingListing).where(
                    AdvertisingListing.id == listing_id,
                    AdvertisingListing.is_active.is_(True),
                    AdvertisingListing.offers_mandatory.is_(True),
                ).with_for_update())).scalar_one_or_none()
                if listing is None or listing.owner_user_id == message.from_user.id:
                    await state.clear()
                    await message.answer("Объявление больше недоступно.")
                    return
                if isinstance(existing_deal_id, int):
                    deal = (await session.execute(select(AdvertisingDeal).where(
                        AdvertisingDeal.id == existing_deal_id,
                        AdvertisingDeal.buyer_user_id == message.from_user.id,
                        AdvertisingDeal.status == "draft_mandatory",
                        AdvertisingDeal.requested_mandatory.is_(True),
                    ).with_for_update())).scalar_one_or_none()
                    if deal is None:
                        await state.clear()
                        await message.answer("Черновик заявки больше недоступен.")
                        return
                    placement = (await session.execute(select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "mandatory",
                    ).with_for_update())).scalar_one_or_none()
                    if placement is None:
                        placement = AdvertisingPlacement(deal_id=deal.id, kind="mandatory", status="ready", config_json={})
                        session.add(placement)
                else:
                    existing = (await session.execute(select(AdvertisingDeal.id).where(
                        AdvertisingDeal.listing_id == listing.id,
                        AdvertisingDeal.buyer_user_id == message.from_user.id,
                        AdvertisingDeal.status == "pending",
                        AdvertisingDeal.requested_mandatory.is_(True),
                    ).limit(1))).scalar_one_or_none()
                    if existing is not None:
                        await state.clear()
                        await message.answer("У вас уже есть заявка на ОП по этой площадке, ожидающая решения.")
                        return
                    deal = AdvertisingDeal(
                        listing_id=listing.id,
                        seller_user_id=listing.owner_user_id,
                        buyer_user_id=message.from_user.id,
                        requested_post=False,
                        requested_mandatory=True,
                        status="draft_mandatory",
                        agreed_terms_json={},
                    )
                    session.add(deal)
                    await session.flush()
                    placement = AdvertisingPlacement(deal_id=deal.id, kind="mandatory", status="ready", config_json={})
                    session.add(placement)

                terms = dict(deal.agreed_terms_json or {})
                terms.update({
                    "mandatory_price_stars": price,
                    "mandatory_terms": listing.mandatory_terms_json,
                    "mandatory_mode": mode,
                    "mandatory_quantity": qty,
                    "mandatory_total_price_stars": total_price,
                })
                deal.agreed_terms_json = terms
                deal.status = "pending"
                placement.status = "ready"
                placement.config_json = {
                    "price_stars": price,
                    "terms": listing.mandatory_terms_json,
                    "mode": mode,
                    "quantity": qty,
                    "total_price_stars": total_price,
                    "target_chat_id": target_chat_id,
                    "target_title": target_title,
                    "target_username": target_username,
                    "target_url": target_url,
                    "target_member_count_at_request": member_count,
                }
                deal_id = deal.id
                seller_id = deal.seller_user_id
                seller_group = listing.group_title_snapshot
                has_post = bool(deal.requested_post)

        await state.clear()
        buyer_name = escape(message.from_user.full_name)
        request_label = "Пост + ОП" if has_post else "ОП"
        volume_text = f"{qty} дн." if mode == "days" else f"{qty} подписчиков"
        username_line = f"🔗 @{escape(target_username)}\n" if target_username else ""
        seller_text = (
            f"📥 <b>Новая заявка: {request_label}</b>\n\n"
            f"🏠 Ваша площадка: <b>{escape(seller_group)}</b>\n"
            f"👤 Покупатель: <b>{buyer_name}</b>\n\n"
            "🎯 <b>Куда вести обязательную подписку:</b>\n"
            f"🏠 <a href=\"{target_url}\">{escape(target_title)}</a>\n"
            f"{username_line}"
            f"👥 Участников: <b>{member_count:,}</b>\n\n".replace(",", " ")
            + f"📐 Объём ОП: <b>{volume_text}</b>\n"
            + f"⭐ Стоимость: <b>{total_price} ⭐</b>\n\n"
            + "После одобрения ОП включится автоматически в вашей группе."
        )
        try:
            await bot.send_message(seller_id, seller_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=_seller_keyboard(deal_id, message.from_user.id, has_post=has_post))
        except Exception:
            pass
        await message.answer(
            "✅ <b>Заявка отправлена рекламодателю</b>\n\n"
            f"📌 Формат: <b>{request_label}</b>\n"
            f"🎯 Группа/канал: <a href=\"{target_url}\">{escape(target_title)}</a>\n"
            f"📐 Объём: <b>{volume_text}</b>\n"
            f"⭐ Итоговая стоимость: <b>{total_price} ⭐</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]),
        )

    @router.callback_query(F.data.regexp(r"^ads:mandatory:cancel:\d+$"))
    async def cancel_draft(callback: CallbackQuery, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is not None and deal.buyer_user_id == callback.from_user.id and deal.status in {"draft_post", "draft_mandatory"}:
                    deal.status = "cancelled"
                    for placement in (await session.execute(select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id).with_for_update())).scalars().all():
                        if placement.status in {"draft", "ready", "pending"}:
                            placement.status = "cancelled"
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text("❌ Рекламная заявка отменена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")]]))
        await callback.answer("Заявка отменена")

    @router.callback_query(F.data.regexp(r"^ads:mandatory:accept:\d+$"))
    async def accept(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        buyer_id: int | None = None
        target_title = ""
        target_url = ""
        seller_group = ""
        post_started = False
        duration_days = 1
        mode = "days"
        quantity = 1
        total_price = 0
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                used_op = int((await session.execute(select(func.count()).select_from(AdvertisingDeal).where(
                    AdvertisingDeal.listing_id == deal.listing_id,
                    AdvertisingDeal.accepted_at >= day_start,
                    AdvertisingDeal.requested_mandatory.is_(True),
                ))).scalar_one())
                if used_op >= MANDATORY_DAILY_LIMIT:
                    await callback.answer("Лимит ОП на сегодня уже использован: 3 из 3.", show_alert=True)
                    return
                if deal.requested_post:
                    used_post = int((await session.execute(select(func.count()).select_from(AdvertisingDeal).where(
                        AdvertisingDeal.listing_id == deal.listing_id,
                        AdvertisingDeal.accepted_at >= day_start,
                        AdvertisingDeal.requested_post.is_(True),
                    ))).scalar_one())
                    if used_post >= POST_DAILY_LIMIT:
                        await callback.answer("Лимит рекламных постов на сегодня уже использован: 1 из 1.", show_alert=True)
                        return
                mandatory = (await session.execute(select(AdvertisingPlacement).where(
                    AdvertisingPlacement.deal_id == deal.id,
                    AdvertisingPlacement.kind == "mandatory",
                ).with_for_update())).scalar_one_or_none()
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id))).scalar_one_or_none()
                if mandatory is None or mandatory.status != "ready" or listing is None:
                    await callback.answer("Данные ОП не готовы.", show_alert=True)
                    return
                cfg = dict(mandatory.config_json or {})
                target_chat_id = cfg.get("target_chat_id")
                if not isinstance(target_chat_id, int):
                    await callback.answer("Группа ОП не определена.", show_alert=True)
                    return
                mode = str(cfg.get("mode") or "days")
                quantity = max(int(cfg.get("quantity") or 1), 1)
                total_price = int(cfg.get("total_price_stars") or 0)
                target_title = str(cfg.get("target_title") or "Группа")
                target_url = str(cfg.get("target_url") or "")
                if mode == "days":
                    mandatory.ends_at = now + timedelta(days=quantity)
                else:
                    try:
                        baseline = await bot.get_chat_member_count(target_chat_id)
                    except Exception:
                        baseline = int(cfg.get("target_member_count_at_request") or 0)
                    cfg["baseline_member_count"] = baseline
                    cfg["target_member_count"] = baseline + quantity
                    mandatory.ends_at = None
                cfg["started_at"] = now.isoformat()
                mandatory.config_json = cfg
                mandatory.status = "active"
                mandatory.starts_at = now

                if deal.requested_post:
                    post = (await session.execute(select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "post",
                    ).with_for_update())).scalar_one_or_none()
                    if post is None or post.status != "ready":
                        await callback.answer("Рекламный пост не готов.", show_alert=True)
                        return
                    duration_days = _duration_days(deal)
                    post.status = "active"
                    post.starts_at = now
                    post.ends_at = now + timedelta(days=duration_days)
                    post_cfg = dict(post.config_json or {})
                    post_cfg["duration_days"] = duration_days
                    post.config_json = post_cfg
                    post_started = True

                deal.status = "accepted"
                deal.accepted_at = now
                deal.started_at = now
                buyer_id = deal.buyer_user_id
                seller_group = listing.group_title_snapshot

        volume_text = f"{quantity} дн." if mode == "days" else f"{quantity} подписчиков"
        if buyer_id is not None:
            try:
                extra = f"\n📣 Рекламный пост также запущен на <b>{duration_days} дн.</b>" if post_started else ""
                await bot.send_message(
                    buyer_id,
                    "✅ <b>Рекламодатель одобрил вашу заявку</b>\n\n"
                    f"🏠 Площадка: <b>{escape(seller_group)}</b>\n"
                    f"🎯 ОП на: <a href=\"{target_url}\">{escape(target_title)}</a>\n"
                    f"📐 Объём ОП: <b>{volume_text}</b>\n"
                    f"⭐ Стоимость ОП: <b>{total_price} ⭐</b>\n\n"
                    f"🚀 Обязательная подписка включена автоматически.{extra}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        if callback.message is not None:
            extra = f"\n📣 Пост: <b>запущен на {duration_days} дн.</b>" if post_started else ""
            await callback.message.edit_text(
                "✅ <b>Заявка одобрена и запущена</b>\n\n"
                f"🎯 ОП: <a href=\"{target_url}\">{escape(target_title)}</a>\n"
                f"📐 Объём: <b>{volume_text}</b>{extra}\n\n"
                "Обычные участники вашей группы должны быть подписаны на указанную площадку, чтобы писать сообщения.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await callback.answer("Реклама запущена")

    @router.callback_query(F.data.regexp(r"^ads:mandatory:reject:\d+$"))
    async def reject(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        buyer_id = None
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                deal.status = "rejected"
                deal.rejected_at = datetime.now(timezone.utc)
                placements = (await session.execute(select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id).with_for_update())).scalars().all()
                for placement in placements:
                    if placement.status in {"draft", "ready", "pending"}:
                        placement.status = "rejected"
                buyer_id = deal.buyer_user_id
        if buyer_id is not None:
            try:
                await bot.send_message(buyer_id, "❌ Рекламодатель отклонил вашу рекламную заявку.")
            except Exception:
                pass
        if callback.message is not None:
            await callback.message.edit_text("❌ Рекламная заявка отклонена.")
        await callback.answer("Заявка отклонена")

    return router
