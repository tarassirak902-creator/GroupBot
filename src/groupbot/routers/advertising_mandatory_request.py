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

MANDATORY_DAILY_LIMIT = 3
POST_DAILY_LIMIT = 1


class MandatoryRequestState(StatesGroup):
    waiting_target = State()


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


async def _set_target_state(
    state: FSMContext,
    *,
    listing_id: int | None = None,
    deal_id: int | None = None,
) -> None:
    await state.set_state(MandatoryRequestState.waiting_target)
    payload: dict[str, int] = {}
    if listing_id is not None:
        payload["mandatory_listing_id"] = listing_id
    if deal_id is not None:
        payload["mandatory_existing_deal_id"] = deal_id
    await state.update_data(**payload)


def create_advertising_mandatory_request_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_mandatory_request")

    @router.callback_query(F.data.regexp(r"^ads:req:type:\d+:mandatory$"))
    async def start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        listing_id = int((callback.data or "").split(":")[3])
        async with session_factory() as session:
            listing = (await session.execute(
                select(AdvertisingListing).where(
                    AdvertisingListing.id == listing_id,
                    AdvertisingListing.is_active.is_(True),
                    AdvertisingListing.offers_mandatory.is_(True),
                )
            )).scalar_one_or_none()
        if listing is None or listing.owner_user_id == callback.from_user.id:
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        await state.clear()
        await _set_target_state(state, listing_id=listing_id)
        await callback.message.edit_text(
            "✅ <b>Покупка обязательной подписки</b>\n\n"
            "Сначала укажите группу или канал, на который должны подписываться участники площадки рекламодателя.\n\n"
            "Отправьте публичный <b>@username</b> или ссылку <b>https://t.me/...</b>.\n\n"
            "⚠️ Mimorus должен быть администратором в этой группе/канале, чтобы проверять подписку пользователей.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data="ads:buy")]
            ]),
        )
        await callback.answer()

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
                deal = (await session.execute(
                    select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
                )).scalar_one_or_none()
                if deal is None or deal.buyer_user_id != callback.from_user.id or deal.status != "draft_post":
                    await callback.answer("Черновик недоступен.", show_alert=True)
                    return
                listing = (await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id)
                )).scalar_one_or_none()
                post = (await session.execute(
                    select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "post",
                    ).with_for_update()
                )).scalar_one_or_none()
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
                if requested_mandatory:
                    deal.status = "draft_mandatory"
                else:
                    deal.status = "pending"

        await state.clear()
        if requested_mandatory:
            await _set_target_state(state, deal_id=deal_id)
            await callback.message.answer(
                "✅ <b>Рекламный пост готов</b>\n\n"
                "Теперь укажите группу или канал для обязательной подписки.\n"
                "Отправьте публичный @username или ссылку https://t.me/...\n\n"
                "Mimorus должен быть администратором в этой группе/канале.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отменить заявку", callback_data=f"ads:deal:cancel_ask:{deal_id}")]
                ]),
            )
            await callback.answer("Теперь укажите площадку для ОП")
            return

        await callback.message.answer(
            "✅ <b>Рекламный пост отправлен рекламодателю на рассмотрение</b>\n\n"
            f"🏠 Площадка: <b>{escape(title)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")]
            ]),
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
        data = await state.get_data()
        listing_id = data.get("mandatory_listing_id")
        existing_deal_id = data.get("mandatory_existing_deal_id")
        ref = _target_ref(message.text)
        try:
            await message.delete()
        except Exception:
            pass
        if ref is None or (not isinstance(listing_id, int) and not isinstance(existing_deal_id, int)):
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
            await message.answer(
                "Не удалось проверить эту группу/канал. Убедитесь, что ссылка публичная и Mimorus добавлен туда администратором."
            )
            return

        username = getattr(target_chat, "username", None)
        if not username:
            await message.answer("Для ОП сейчас нужна публичная группа/канал с @username.")
            return
        target_url = f"https://t.me/{username}"
        target_title = target_chat.title or f"@{username}"

        async with session_factory() as session:
            async with session.begin():
                if isinstance(existing_deal_id, int):
                    deal = (await session.execute(
                        select(AdvertisingDeal).where(
                            AdvertisingDeal.id == existing_deal_id,
                            AdvertisingDeal.buyer_user_id == message.from_user.id,
                            AdvertisingDeal.status == "draft_mandatory",
                            AdvertisingDeal.requested_mandatory.is_(True),
                        ).with_for_update()
                    )).scalar_one_or_none()
                    if deal is None:
                        await state.clear()
                        await message.answer("Черновик заявки больше недоступен.")
                        return
                    listing = (await session.execute(
                        select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id)
                    )).scalar_one_or_none()
                    if listing is None:
                        await state.clear()
                        await message.answer("Объявление больше недоступно.")
                        return
                    placement = (await session.execute(
                        select(AdvertisingPlacement).where(
                            AdvertisingPlacement.deal_id == deal.id,
                            AdvertisingPlacement.kind == "mandatory",
                        ).with_for_update()
                    )).scalar_one_or_none()
                    if placement is None:
                        placement = AdvertisingPlacement(deal_id=deal.id, kind="mandatory", status="ready", config_json={})
                        session.add(placement)
                    deal.status = "pending"
                else:
                    listing = (await session.execute(
                        select(AdvertisingListing).where(
                            AdvertisingListing.id == listing_id,
                            AdvertisingListing.is_active.is_(True),
                            AdvertisingListing.offers_mandatory.is_(True),
                        ).with_for_update()
                    )).scalar_one_or_none()
                    if listing is None or listing.owner_user_id == message.from_user.id:
                        await state.clear()
                        await message.answer("Объявление больше недоступно.")
                        return
                    existing = (await session.execute(
                        select(AdvertisingDeal.id).where(
                            AdvertisingDeal.listing_id == listing.id,
                            AdvertisingDeal.buyer_user_id == message.from_user.id,
                            AdvertisingDeal.status == "pending",
                            AdvertisingDeal.requested_mandatory.is_(True),
                        ).limit(1)
                    )).scalar_one_or_none()
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
                        status="pending",
                        agreed_terms_json={
                            "mandatory_price_stars": listing.mandatory_price_stars,
                            "mandatory_terms": listing.mandatory_terms_json,
                        },
                    )
                    session.add(deal)
                    await session.flush()
                    placement = AdvertisingPlacement(deal_id=deal.id, kind="mandatory", status="ready", config_json={})
                    session.add(placement)

                placement.status = "ready"
                placement.config_json = {
                    "price_stars": listing.mandatory_price_stars,
                    "terms": listing.mandatory_terms_json,
                    "target_chat_id": target_chat.id,
                    "target_title": target_title,
                    "target_username": username,
                    "target_url": target_url,
                    "target_member_count": member_count,
                }
                deal_id = deal.id
                seller_id = deal.seller_user_id
                seller_group = listing.group_title_snapshot
                has_post = bool(deal.requested_post)

        await state.clear()
        buyer_name = escape(message.from_user.full_name)
        request_label = "Пост + ОП" if has_post else "ОП"
        seller_text = (
            f"📥 <b>Новая заявка: {request_label}</b>\n\n"
            f"🏠 Ваша площадка: <b>{escape(seller_group)}</b>\n"
            f"👤 Покупатель: <b>{buyer_name}</b>\n\n"
            "🎯 <b>Куда вести обязательную подписку:</b>\n"
            f"🏠 <a href=\"{target_url}\">{escape(target_title)}</a>\n"
            f"🔗 @{escape(username)}\n"
            f"👥 Участников: <b>{member_count:,}</b>\n\n".replace(",", " ") +
            "После одобрения ОП включится автоматически в вашей группе."
        )
        try:
            await bot.send_message(
                seller_id,
                seller_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_seller_keyboard(deal_id, message.from_user.id, has_post=has_post),
            )
        except Exception:
            pass
        await message.answer(
            "✅ <b>Заявка отправлена рекламодателю</b>\n\n"
            f"📌 Формат: <b>{request_label}</b>\n"
            f"🎯 Группа/канал: <a href=\"{target_url}\">{escape(target_title)}</a>\n"
            f"👥 Участников: <b>{member_count:,}</b>".replace(",", " "),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]),
        )

    @router.callback_query(F.data.regexp(r"^ads:mandatory:accept:\d+$"))
    async def accept(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        buyer_id = None
        target_title = ""
        target_url = ""
        seller_group = ""
        post_started = False
        duration_days = 1
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(
                    select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
                )).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                used_op = int((await session.execute(
                    select(func.count()).select_from(AdvertisingDeal).where(
                        AdvertisingDeal.listing_id == deal.listing_id,
                        AdvertisingDeal.accepted_at >= day_start,
                        AdvertisingDeal.requested_mandatory.is_(True),
                    )
                )).scalar_one())
                if used_op >= MANDATORY_DAILY_LIMIT:
                    await callback.answer("Лимит ОП на сегодня уже использован: 3 из 3.", show_alert=True)
                    return
                if deal.requested_post:
                    used_post = int((await session.execute(
                        select(func.count()).select_from(AdvertisingDeal).where(
                            AdvertisingDeal.listing_id == deal.listing_id,
                            AdvertisingDeal.accepted_at >= day_start,
                            AdvertisingDeal.requested_post.is_(True),
                        )
                    )).scalar_one())
                    if used_post >= POST_DAILY_LIMIT:
                        await callback.answer("Лимит рекламных постов на сегодня уже использован: 1 из 1.", show_alert=True)
                        return

                mandatory = (await session.execute(
                    select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "mandatory",
                    ).with_for_update()
                )).scalar_one_or_none()
                listing = (await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id)
                )).scalar_one_or_none()
                if mandatory is None or mandatory.status != "ready" or listing is None:
                    await callback.answer("Данные ОП не готовы.", show_alert=True)
                    return

                mandatory.status = "active"
                mandatory.starts_at = now
                cfg = dict(mandatory.config_json or {})
                target_title = str(cfg.get("target_title") or "Группа")
                target_url = str(cfg.get("target_url") or "")

                if deal.requested_post:
                    post = (await session.execute(
                        select(AdvertisingPlacement).where(
                            AdvertisingPlacement.deal_id == deal.id,
                            AdvertisingPlacement.kind == "post",
                        ).with_for_update()
                    )).scalar_one_or_none()
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

        if buyer_id is not None:
            try:
                extra = f"\n📣 Рекламный пост также запущен на <b>{duration_days} дн.</b>" if post_started else ""
                await bot.send_message(
                    buyer_id,
                    "✅ <b>Рекламодатель одобрил вашу заявку</b>\n\n"
                    f"🏠 Площадка: <b>{escape(seller_group)}</b>\n"
                    f"🎯 ОП на: <a href=\"{target_url}\">{escape(target_title)}</a>\n\n"
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
                f"🎯 ОП: <a href=\"{target_url}\">{escape(target_title)}</a>{extra}\n\n"
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
                deal = (await session.execute(
                    select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
                )).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                deal.status = "rejected"
                deal.rejected_at = datetime.now(timezone.utc)
                placements = (await session.execute(
                    select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id).with_for_update()
                )).scalars().all()
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
