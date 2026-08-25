from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement


def _request_type_keyboard(listing: AdvertisingListing) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if listing.offers_post:
        rows.append([InlineKeyboardButton(text="📣 Рекламный пост", callback_data=f"ads:req:type:{listing.id}:post")])
    if listing.offers_mandatory:
        rows.append([InlineKeyboardButton(text="✅ Обязательная подписка", callback_data=f"ads:req:type:{listing.id}:mandatory")])
    if listing.offers_post and listing.offers_mandatory:
        rows.append([InlineKeyboardButton(text="📣 + ✅ Пост и ОП", callback_data=f"ads:req:type:{listing.id}:both")])
    rows.append([InlineKeyboardButton(text="◀️ К объявлению", callback_data=f"ads:listing:{listing.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _seller_request_keyboard(deal_id: int, buyer_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Связаться с покупателем", url=f"tg://user?id={buyer_user_id}")],
        [InlineKeyboardButton(text="📥 Открыть заявку", callback_data=f"ads:deal:{deal_id}")],
    ])


def _can_buyer_cancel(deal: AdvertisingDeal) -> bool:
    return deal.status in {"pending", "accepted"} and deal.started_at is None


def _deal_keyboard(deal: AdvertisingDeal, viewer_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    other_id = deal.buyer_user_id if viewer_id == deal.seller_user_id else deal.seller_user_id
    rows.append([InlineKeyboardButton(text="💬 Связаться", url=f"tg://user?id={other_id}")])
    if viewer_id == deal.seller_user_id and deal.status == "pending":
        rows.append([
            InlineKeyboardButton(text="✅ Принять", callback_data=f"ads:deal:accept:{deal.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:deal:reject:{deal.id}"),
        ])
    if viewer_id == deal.buyer_user_id and deal.status == "accepted":
        rows.append([InlineKeyboardButton(text="📦 Передать материалы", callback_data=f"ads:materials:{deal.id}")])
    if viewer_id == deal.buyer_user_id and _can_buyer_cancel(deal):
        rows.append([InlineKeyboardButton(text="❌ Отменить заявку", callback_data=f"ads:deal:cancel_ask:{deal.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kind_text(deal: AdvertisingDeal) -> str:
    if deal.requested_post and deal.requested_mandatory:
        return "📣 Рекламный пост + ✅ ОП"
    if deal.requested_post:
        return "📣 Рекламный пост"
    return "✅ Обязательная подписка"


def _deal_text(deal: AdvertisingDeal, listing: AdvertisingListing) -> str:
    status = {
        "pending": "⏳ Ожидает решения рекламодателя",
        "accepted": "✅ Принята — ожидает материалов/запуска",
        "rejected": "❌ Отклонена",
        "cancelled": "🚫 Отменена покупателем",
    }.get(deal.status, deal.status)
    return (
        "📨 <b>Рекламная заявка</b>\n\n"
        f"🏠 Площадка: <b>{listing.group_title_snapshot}</b>\n"
        f"📌 Запрос: <b>{_kind_text(deal)}</b>\n"
        f"Статус: <b>{status}</b>"
    )


def create_advertising_requests_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_requests")

    async def _load_listing(listing_id: int) -> AdvertisingListing | None:
        async with session_factory() as session:
            return (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id))).scalar_one_or_none()

    async def _load_deal(deal_id: int) -> tuple[AdvertisingDeal, AdvertisingListing] | None:
        async with session_factory() as session:
            row = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(AdvertisingDeal.id == deal_id)
            )).first()
            return row if row is not None else None

    @router.callback_query(F.data.startswith("ads:request:"))
    async def choose_request_type(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        listing = await _load_listing(listing_id)
        if listing is None or not listing.is_active or listing.owner_user_id == callback.from_user.id:
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        await callback.message.edit_text(
            "📨 <b>Отправить запрос</b>\n\nНа какой вид рекламы отправить заявку?",
            parse_mode="HTML",
            reply_markup=_request_type_keyboard(listing),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:req:type:"))
    async def send_request(callback: CallbackQuery, bot: Bot) -> None:
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
                listing = (await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == listing_id).with_for_update()
                )).scalar_one_or_none()
                if listing is None or not listing.is_active or listing.owner_user_id == callback.from_user.id:
                    await callback.answer("Объявление недоступно.", show_alert=True)
                    return
                requested_post = kind in {"post", "both"}
                requested_mandatory = kind in {"mandatory", "both"}
                if requested_post and not listing.offers_post:
                    await callback.answer("Посты в этом объявлении больше не продаются.", show_alert=True)
                    return
                if requested_mandatory and not listing.offers_mandatory:
                    await callback.answer("ОП в этом объявлении больше не продаётся.", show_alert=True)
                    return
                existing = (await session.execute(
                    select(AdvertisingDeal).where(
                        AdvertisingDeal.listing_id == listing_id,
                        AdvertisingDeal.buyer_user_id == callback.from_user.id,
                        AdvertisingDeal.status == "pending",
                    )
                )).scalar_one_or_none()
                if existing is not None:
                    await callback.answer("У вас уже есть заявка на эту площадку, ожидающая решения.", show_alert=True)
                    return
                deal = AdvertisingDeal(
                    listing_id=listing.id,
                    seller_user_id=listing.owner_user_id,
                    buyer_user_id=callback.from_user.id,
                    requested_post=requested_post,
                    requested_mandatory=requested_mandatory,
                    status="pending",
                    agreed_terms_json={
                        "post_price_stars": listing.post_price_stars if requested_post else None,
                        "post_interval_minutes": listing.post_interval_minutes if requested_post else None,
                        "post_terms": listing.post_terms_json if requested_post else None,
                        "mandatory_price_stars": listing.mandatory_price_stars if requested_mandatory else None,
                        "mandatory_terms": listing.mandatory_terms_json if requested_mandatory else None,
                    },
                )
                session.add(deal)
                await session.flush()
                deal_id = deal.id
                seller_id = deal.seller_user_id
                title = listing.group_title_snapshot
                kind_label = _kind_text(deal)

        buyer_name = callback.from_user.full_name
        try:
            await bot.send_message(
                seller_id,
                "📥 <b>Новая рекламная заявка</b>\n\n"
                f"🏠 Площадка: <b>{title}</b>\n"
                f"👤 Покупатель: <b>{buyer_name}</b>\n"
                f"📌 Запрос: <b>{kind_label}</b>\n\n"
                "Вы можете связаться с покупателем и обсудить условия перед принятием заявки.",
                parse_mode="HTML",
                reply_markup=_seller_request_keyboard(deal_id, callback.from_user.id),
            )
        except Exception:
            pass

        loaded = await _load_deal(deal_id)
        if loaded is None:
            return
        deal, listing = loaded
        await callback.message.edit_text(
            "✅ <b>Заявка отправлена</b>\n\n" + _deal_text(deal, listing),
            parse_mode="HTML",
            reply_markup=_deal_keyboard(deal, callback.from_user.id),
        )
        await callback.answer("Заявка отправлена")

    @router.callback_query(F.data.startswith("ads:deal:cancel_ask:"))
    async def ask_cancel_deal(callback: CallbackQuery) -> None:
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная заявка.", show_alert=True)
            return
        loaded = await _load_deal(deal_id)
        if loaded is None:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        deal, listing = loaded
        if callback.from_user.id != deal.buyer_user_id:
            await callback.answer("Отменить заявку может только покупатель.", show_alert=True)
            return
        if not _can_buyer_cancel(deal):
            await callback.answer("Эту заявку уже нельзя отменить: реклама запущена или сделка завершена.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(
                "⚠️ <b>Отменить рекламную заявку?</b>\n\n" + _deal_text(deal, listing) +
                "\n\nПосле отмены заявка останется в истории, но больше не будет активной.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Да, отменить", callback_data=f"ads:deal:cancel:{deal.id}")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:deal:{deal.id}")],
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:deal:cancel:"))
    async def cancel_deal(callback: CallbackQuery, bot: Bot) -> None:
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная заявка.", show_alert=True)
            return
        seller_id = None
        title = ""
        kind_label = ""
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None:
                    await callback.answer("Заявка не найдена.", show_alert=True)
                    return
                if callback.from_user.id != deal.buyer_user_id:
                    await callback.answer("Отменить заявку может только покупатель.", show_alert=True)
                    return
                if not _can_buyer_cancel(deal):
                    await callback.answer("Эту заявку уже нельзя отменить: реклама запущена или сделка завершена.", show_alert=True)
                    return
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id))).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Объявление не найдено.", show_alert=True)
                    return
                deal.status = "cancelled"
                # Preserve the deal row for history/audit. Only unfinished placements are cancelled.
                placements = (await session.execute(select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal.id).with_for_update())).scalars().all()
                for placement in placements:
                    if placement.status in {"draft", "ready", "pending"}:
                        placement.status = "cancelled"
                terms = dict(deal.agreed_terms_json or {})
                terms["cancelled_by"] = "buyer"
                terms["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                deal.agreed_terms_json = terms
                seller_id = deal.seller_user_id
                title = listing.group_title_snapshot
                kind_label = _kind_text(deal)
        if seller_id is not None:
            try:
                await bot.send_message(
                    seller_id,
                    "🚫 <b>Покупатель отменил рекламную заявку</b>\n\n"
                    f"🏠 Площадка: <b>{title}</b>\n"
                    f"📌 Формат: <b>{kind_label}</b>\n\n"
                    "Реклама не была запущена. Заявка сохранена в истории.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        loaded = await _load_deal(deal_id)
        if loaded is not None and callback.message is not None:
            deal, listing = loaded
            await callback.message.edit_text(
                _deal_text(deal, listing) + "\n\nЗаявка отменена. Реклама не будет запущена.",
                parse_mode="HTML",
                reply_markup=_deal_keyboard(deal, callback.from_user.id),
            )
        await callback.answer("Заявка отменена")

    @router.callback_query(F.data.startswith("ads:deal:"))
    async def open_deal(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            return
        try:
            deal_id = int(parts[2])
        except ValueError:
            return
        loaded = await _load_deal(deal_id)
        if loaded is None:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        deal, listing = loaded
        if callback.from_user.id not in {deal.seller_user_id, deal.buyer_user_id}:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(_deal_text(deal, listing), parse_mode="HTML", reply_markup=_deal_keyboard(deal, callback.from_user.id))
        await callback.answer()

    @router.callback_query(F.data == "ads:my_buys")
    async def my_buys(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        async with session_factory() as session:
            rows = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(AdvertisingDeal.buyer_user_id == callback.from_user.id)
                .order_by(AdvertisingDeal.created_at.desc())
                .limit(50)
            )).all()
        buttons = [[InlineKeyboardButton(text=f"{_kind_text(deal)} · {listing.group_title_snapshot}"[:64], callback_data=f"ads:deal:{deal.id}")] for deal, listing in rows]
        buttons.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
        text = "📋 <b>Мои покупки</b>\n\nВыберите заявку:" if rows else "📋 <b>Мои покупки</b>\n\nУ вас пока нет рекламных заявок."
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @router.callback_query(F.data == "ads:my_sales")
    async def my_sales_requests(callback: CallbackQuery) -> None:
        return

    return router
