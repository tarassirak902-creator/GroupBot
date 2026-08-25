from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.routers import advertising_deal_actions_v2 as deal_actions_v2_module
from groupbot.routers import advertising_requests as advertising_requests_module


def _kind_text(deal: AdvertisingDeal) -> str:
    if deal.requested_post and deal.requested_mandatory:
        return "📣+✅"
    if deal.requested_post:
        return "📣"
    return "✅"


def _seller_can_stop(deal: AdvertisingDeal) -> bool:
    return deal.status == "accepted"


def _patched_deal_keyboard(deal: AdvertisingDeal, viewer_id: int) -> InlineKeyboardMarkup:
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
    can_buyer_cancel = deal.status in {"pending", "accepted"} and deal.started_at is None
    if viewer_id == deal.buyer_user_id and can_buyer_cancel:
        rows.append([InlineKeyboardButton(text="❌ Отменить заявку", callback_data=f"ads:deal:cancel_ask:{deal.id}")])
    if viewer_id == deal.seller_user_id and _seller_can_stop(deal):
        rows.append([
            InlineKeyboardButton(
                text="⏹ Остановить и удалить рекламу",
                callback_data=f"ads:sales:stop_ask:{deal.id}",
            )
        ])
    if viewer_id == deal.seller_user_id:
        rows.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
    else:
        rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _patched_after_action_keyboard(deal: AdvertisingDeal, viewer_id: int) -> InlineKeyboardMarkup:
    other_id = deal.buyer_user_id if viewer_id == deal.seller_user_id else deal.seller_user_id
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="💬 Связаться", url=f"tg://user?id={other_id}")]
    ]
    if viewer_id == deal.buyer_user_id and deal.status == "accepted" and deal.requested_mandatory:
        rows.append([InlineKeyboardButton(text="📦 Передать материалы ОП", callback_data=f"ads:materials:{deal.id}")])
    if viewer_id == deal.seller_user_id and _seller_can_stop(deal):
        rows.append([
            InlineKeyboardButton(
                text="⏹ Остановить и удалить рекламу",
                callback_data=f"ads:sales:stop_ask:{deal.id}",
            )
        ])
    if viewer_id == deal.buyer_user_id:
        rows.append([InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")])
    else:
        rows.append([InlineKeyboardButton(text="📦 Мои продажи", callback_data="ads:my_sales")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# advertising_sales_nav is registered before advertising_requests. Patch the common
# deal keyboards here so seller controls are visible both after accepting a deal
# and when a deal is opened later from "Мои продажи".
advertising_requests_module._deal_keyboard = _patched_deal_keyboard
deal_actions_v2_module._after_action_keyboard = _patched_after_action_keyboard


def _hub_keyboard(listing_count: int, pending_count: int, active_count: int, history_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📣 Мои объявления · {listing_count}", callback_data="ads:sales:listings")],
        [InlineKeyboardButton(text=f"📥 Входящие заявки · {pending_count}", callback_data="ads:sales:incoming")],
        [InlineKeyboardButton(text=f"💼 Активные продажи · {active_count}", callback_data="ads:sales:active")],
        [InlineKeyboardButton(text=f"✅ История продаж · {history_count}", callback_data="ads:sales:history")],
        [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
    ])


def _listings_keyboard(rows: list[AdvertisingListing]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for listing in rows:
        kinds: list[str] = []
        if listing.offers_post:
            kinds.append("📣")
        if listing.offers_mandatory:
            kinds.append("✅")
        buttons.append([InlineKeyboardButton(text=f"{''.join(kinds)} {listing.group_title_snapshot}"[:64], callback_data=f"ads:listing:{listing.id}")])
    buttons.append([InlineKeyboardButton(text="➕ Создать/изменить объявление", callback_data="ads:sell")])
    buttons.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _deals_keyboard(rows: list[tuple[AdvertisingDeal, AdvertisingListing]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for deal, listing in rows:
        buttons.append([InlineKeyboardButton(text=f"{_kind_text(deal)} {listing.group_title_snapshot}"[:64], callback_data=f"ads:deal:{deal.id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def create_advertising_sales_nav_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_sales_nav")

    async def _load_counts(user_id: int) -> tuple[int, int, int, int]:
        async with session_factory() as session:
            listings = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.owner_user_id == user_id))).scalars().all()
            deals = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.seller_user_id == user_id))).scalars().all()
        pending = sum(1 for d in deals if d.status == "pending")
        active = sum(1 for d in deals if d.status == "accepted")
        history = max(len(deals) - pending - active, 0)
        return len(listings), pending, active, history

    async def _load_deals(user_id: int, section: str) -> list[tuple[AdvertisingDeal, AdvertisingListing]]:
        async with session_factory() as session:
            rows = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(AdvertisingDeal.seller_user_id == user_id)
                .order_by(AdvertisingDeal.created_at.desc())
                .limit(100)
            )).all()
        if section == "incoming":
            return [(d, l) for d, l in rows if d.status == "pending"]
        if section == "active":
            return [(d, l) for d, l in rows if d.status == "accepted"]
        return [(d, l) for d, l in rows if d.status not in {"pending", "accepted"}]

    @router.callback_query(F.data == "ads:my_sales")
    async def my_sales(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer(); return
        listing_count, pending_count, active_count, history_count = await _load_counts(callback.from_user.id)
        await callback.message.edit_text(
            "📦 <b>Мои продажи</b>\n\nЗдесь находятся ваши рекламные объявления, входящие заявки и все сделки по продаже рекламы.",
            parse_mode="HTML",
            reply_markup=_hub_keyboard(listing_count, pending_count, active_count, history_count),
        )
        await callback.answer()

    @router.callback_query(F.data == "ads:sales:listings")
    async def sales_listings(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer(); return
        async with session_factory() as session:
            rows = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.owner_user_id == callback.from_user.id).order_by(AdvertisingListing.updated_at.desc(), AdvertisingListing.id.desc()))).scalars().all()
        text = "📣 <b>Мои объявления</b>\n\nВыберите рекламную площадку:" if rows else "📣 <b>Мои объявления</b>\n\nУ вас пока нет рекламных объявлений."
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_listings_keyboard(list(rows)))
        await callback.answer()

    async def _show_deal_section(callback: CallbackQuery, section: str, title: str, empty: str) -> None:
        if callback.message is None:
            await callback.answer(); return
        rows = await _load_deals(callback.from_user.id, section)
        text = f"{title}\n\nВыберите сделку:" if rows else f"{title}\n\n{empty}"
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_deals_keyboard(rows))
        await callback.answer()

    @router.callback_query(F.data == "ads:sales:incoming")
    async def incoming(callback: CallbackQuery) -> None:
        await _show_deal_section(callback, "incoming", "📥 <b>Входящие заявки</b>", "Новых заявок сейчас нет.")

    @router.callback_query(F.data == "ads:sales:active")
    async def active(callback: CallbackQuery) -> None:
        await _show_deal_section(callback, "active", "💼 <b>Активные продажи</b>", "Активных продаж сейчас нет.")

    @router.callback_query(F.data == "ads:sales:history")
    async def history(callback: CallbackQuery) -> None:
        await _show_deal_section(callback, "history", "✅ <b>История продаж</b>", "История продаж пока пуста.")

    @router.callback_query(F.data.startswith("ads:sales:stop_ask:"))
    async def stop_ask(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer(); return
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная сделка.", show_alert=True); return
        async with session_factory() as session:
            row = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(AdvertisingDeal.id == deal_id)
            )).first()
        if row is None:
            await callback.answer("Сделка не найдена.", show_alert=True); return
        deal, listing = row
        if deal.seller_user_id != callback.from_user.id:
            await callback.answer("Остановить рекламу может только рекламодатель.", show_alert=True); return
        if not _seller_can_stop(deal):
            await callback.answer("Эта реклама уже не активна.", show_alert=True); return
        await callback.message.edit_text(
            "⚠️ <b>Остановить и удалить рекламу?</b>\n\n"
            f"🏠 Площадка: <b>{listing.group_title_snapshot}</b>\n"
            f"📌 Формат: <b>{_kind_text(deal)}</b>\n\n"
            "Показ рекламных постов и/или обязательная подписка по этой сделке будут остановлены. "
            "Сделка исчезнет из активных продаж и останется в истории.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏹ Да, остановить", callback_data=f"ads:sales:stop:{deal.id}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:deal:{deal.id}")],
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:sales:stop:"))
    async def stop_confirm(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            await callback.answer(); return
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная сделка.", show_alert=True); return
        now = datetime.now(timezone.utc)
        buyer_id: int | None = None
        title = ""
        kind_label = ""
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(
                    select(AdvertisingDeal)
                    .where(AdvertisingDeal.id == deal_id)
                    .with_for_update()
                )).scalar_one_or_none()
                if deal is None:
                    await callback.answer("Сделка не найдена.", show_alert=True); return
                if deal.seller_user_id != callback.from_user.id:
                    await callback.answer("Остановить рекламу может только рекламодатель.", show_alert=True); return
                if not _seller_can_stop(deal):
                    await callback.answer("Эта реклама уже не активна.", show_alert=True); return
                listing = (await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id)
                )).scalar_one_or_none()
                if listing is None:
                    await callback.answer("Рекламная площадка не найдена.", show_alert=True); return
                placements = (await session.execute(
                    select(AdvertisingPlacement)
                    .where(AdvertisingPlacement.deal_id == deal.id)
                    .with_for_update()
                )).scalars().all()
                for placement in placements:
                    if placement.status in {"draft", "pending", "ready", "active"}:
                        placement.status = "cancelled"
                        if placement.ends_at is None or placement.ends_at > now:
                            placement.ends_at = now
                terms = dict(deal.agreed_terms_json or {})
                terms["stopped_by"] = "seller"
                terms["stopped_at"] = now.isoformat()
                deal.agreed_terms_json = terms
                deal.status = "finished_waiting_confirmation"
                deal.finished_at = now
                buyer_id = deal.buyer_user_id
                title = listing.group_title_snapshot
                kind_label = _kind_text(deal)
        if buyer_id is not None:
            try:
                await bot.send_message(
                    buyer_id,
                    "⏹ <b>Рекламодатель остановил размещение</b>\n\n"
                    f"🏠 Площадка: <b>{title}</b>\n"
                    f"📌 Формат: <b>{kind_label}</b>\n\n"
                    "Дальнейший показ рекламы по этой сделке остановлен. Сделка сохранена в истории.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📨 Открыть сделку", callback_data=f"ads:deal:{deal_id}")],
                        [InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")],
                    ]),
                )
            except Exception:
                pass
        await callback.message.edit_text(
            "⏹ <b>Реклама остановлена</b>\n\n"
            f"🏠 Площадка: <b>{title}</b>\n"
            f"📌 Формат: <b>{kind_label}</b>\n\n"
            "Размещение удалено из активных продаж и сохранено в истории сделки.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ История продаж", callback_data="ads:sales:history")],
                [InlineKeyboardButton(text="📦 Мои продажи", callback_data="ads:my_sales")],
            ]),
        )
        await callback.answer("Реклама остановлена")

    return router
