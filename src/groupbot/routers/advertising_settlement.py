from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import (
    AdvertisingDeal,
    AdvertisingDispute,
    AdvertisingNoClaimsConfirmation,
    AdvertisingReview,
)
from groupbot.routers import advertising_requests as advertising_requests_module
from groupbot.routers.advertising_mutual_op import (
    create_advertising_mutual_op_router,
    request_type_keyboard_with_mutual,
)
from groupbot.routers.advertising_mutual_tracking import create_advertising_mutual_tracking_router


class AdvertisingSettlementState(StatesGroup):
    waiting_dispute = State()
    waiting_review_text = State()


def settlement_keyboard(deal_id: int, *, allow_dispute: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✅ Претензий нет", callback_data=f"ads:settle:ok:{deal_id}")]]
    if allow_dispute:
        rows.append([InlineKeyboardButton(text="⚠️ Открыть спор", callback_data=f"ads:settle:dispute:{deal_id}")])
    rows.append([InlineKeyboardButton(text="⭐ Оставить отзыв", callback_data=f"ads:settle:review:{deal_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rating_keyboard(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{n} ⭐", callback_data=f"ads:settle:rate:{deal_id}:{n}") for n in range(1, 6)
    ], [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ads:deal:{deal_id}")]])


def create_advertising_settlement_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_settlement")
    # Keep mutual OP ahead of the legacy generic advertising request router.
    advertising_requests_module._request_type_keyboard = request_type_keyboard_with_mutual
    router.include_router(create_advertising_mutual_op_router(session_factory))
    router.include_router(create_advertising_mutual_tracking_router(session_factory))

    async def _load(deal_id: int) -> AdvertisingDeal | None:
        async with session_factory() as session:
            return (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id))).scalar_one_or_none()

    @router.callback_query(F.data.regexp(r"^ads:settle:ok:\d+$"))
    async def no_claims(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        other_id: int | None = None
        completed = False
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or callback.from_user.id not in {deal.buyer_user_id, deal.seller_user_id}:
                    await callback.answer("Сделка недоступна.", show_alert=True); return
                if deal.status != "finished_waiting_confirmation":
                    await callback.answer("Эту сделку уже нельзя закрыть этим способом.", show_alert=True); return
                dispute = (await session.execute(select(AdvertisingDispute.id).where(AdvertisingDispute.deal_id == deal.id, AdvertisingDispute.status == "open"))).scalar_one_or_none()
                if dispute is not None:
                    await callback.answer("По сделке открыт спор.", show_alert=True); return
                existing = (await session.execute(select(AdvertisingNoClaimsConfirmation).where(AdvertisingNoClaimsConfirmation.deal_id == deal.id, AdvertisingNoClaimsConfirmation.user_id == callback.from_user.id))).scalar_one_or_none()
                if existing is None:
                    session.add(AdvertisingNoClaimsConfirmation(deal_id=deal.id, user_id=callback.from_user.id))
                confirmations = list((await session.execute(select(AdvertisingNoClaimsConfirmation.user_id).where(AdvertisingNoClaimsConfirmation.deal_id == deal.id))).scalars().all())
                if callback.from_user.id not in confirmations:
                    confirmations.append(callback.from_user.id)
                if len(set(confirmations)) >= 2:
                    deal.status = "completed_mutual"
                    deal.completed_at = now
                    completed = True
                elif deal.first_no_claims_at is None:
                    deal.first_no_claims_at = now
                    deal.no_claims_deadline_at = now + timedelta(hours=5)
                other_id = deal.seller_user_id if callback.from_user.id == deal.buyer_user_id else deal.buyer_user_id
        if completed:
            if callback.message is not None:
                await callback.message.edit_text("✅ <b>Сделка завершена</b>\n\nОбе стороны подтвердили отсутствие претензий. Спор по этой сделке больше открыть нельзя.\n\nОтзыв и оценку оставить можно.", parse_mode="HTML", reply_markup=settlement_keyboard(deal_id, allow_dispute=False))
            if other_id is not None:
                try: await bot.send_message(other_id, "✅ Сделка закрыта: обе стороны подтвердили отсутствие претензий.", reply_markup=settlement_keyboard(deal_id, allow_dispute=False))
                except Exception: pass
            await callback.answer("Сделка завершена")
        else:
            if callback.message is not None:
                await callback.message.edit_text("✅ <b>Вы подтвердили, что претензий нет.</b>\n\nУ второй стороны есть 5 часов. Если она не выберет действие, сделка закроется автоматически. Отзыв можно оставить и после закрытия.", parse_mode="HTML", reply_markup=settlement_keyboard(deal_id))
            if other_id is not None:
                try: await bot.send_message(other_id, "⏳ Вторая сторона отметила «Претензий нет». У вас есть 5 часов, чтобы подтвердить или открыть спор.", reply_markup=settlement_keyboard(deal_id))
                except Exception: pass
            await callback.answer("Подтверждение принято")

    @router.callback_query(F.data.regexp(r"^ads:settle:dispute:\d+$"))
    async def dispute_start(callback: CallbackQuery, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        deal = await _load(deal_id)
        if deal is None or callback.from_user.id not in {deal.buyer_user_id, deal.seller_user_id} or deal.status not in {"accepted", "finished_waiting_confirmation"}:
            await callback.answer("Спор по этой сделке открыть нельзя.", show_alert=True); return
        await state.set_state(AdvertisingSettlementState.waiting_dispute)
        await state.update_data(settlement_deal_id=deal_id)
        await callback.message.answer("⚠️ Опишите причину спора одним сообщением. Сообщение будет сохранено для разбора сделки.")
        await callback.answer()

    @router.message(AdvertisingSettlementState.waiting_dispute, F.chat.type == "private")
    async def dispute_save(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not message.text: return
        deal_id = (await state.get_data()).get("settlement_deal_id")
        if not isinstance(deal_id, int): await state.clear(); return
        other_id: int | None = None
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or message.from_user.id not in {deal.buyer_user_id, deal.seller_user_id} or deal.status not in {"accepted", "finished_waiting_confirmation"}:
                    await state.clear(); await message.answer("Спор уже нельзя открыть."); return
                existing = (await session.execute(select(AdvertisingDispute).where(AdvertisingDispute.deal_id == deal.id))).scalar_one_or_none()
                if existing is None:
                    session.add(AdvertisingDispute(deal_id=deal.id, opened_by_user_id=message.from_user.id, status="open", reason="user_claim", description=message.text[:4000]))
                elif existing.status != "open":
                    await state.clear(); await message.answer("По этой сделке спор уже закрыт."); return
                deal.status = "dispute_open"
                other_id = deal.seller_user_id if message.from_user.id == deal.buyer_user_id else deal.buyer_user_id
        await state.clear()
        await message.answer("⚠️ <b>Спор открыт</b>\n\nСделка больше не может закрыться через «Претензий нет», пока спор не будет разрешён.", parse_mode="HTML")
        if other_id is not None:
            try: await bot.send_message(other_id, "⚠️ По рекламной сделке открыт спор. Сделка передана на разбор.")
            except Exception: pass

    @router.callback_query(F.data.regexp(r"^ads:settle:review:\d+$"))
    async def review_start(callback: CallbackQuery) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        deal = await _load(deal_id)
        if deal is None or callback.from_user.id not in {deal.buyer_user_id, deal.seller_user_id} or deal.finished_at is None:
            await callback.answer("Отзыв можно оставить после завершения рекламы.", show_alert=True); return
        async with session_factory() as session:
            exists = (await session.execute(select(AdvertisingReview.id).where(AdvertisingReview.deal_id == deal_id, AdvertisingReview.reviewer_user_id == callback.from_user.id))).scalar_one_or_none()
        if exists is not None:
            await callback.answer("Вы уже оставили отзыв по этой сделке.", show_alert=True); return
        await callback.message.answer("⭐ Оцените вторую сторону сделки:", reply_markup=_rating_keyboard(deal_id))
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:settle:rate:\d+:[1-5]$"))
    async def review_rate(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":")
        deal_id, rating = int(parts[3]), int(parts[4])
        deal = await _load(deal_id)
        if deal is None or callback.from_user.id not in {deal.buyer_user_id, deal.seller_user_id} or deal.finished_at is None:
            await callback.answer("Отзыв недоступен.", show_alert=True); return
        await state.set_state(AdvertisingSettlementState.waiting_review_text)
        await state.update_data(settlement_deal_id=deal_id, settlement_rating=rating)
        await callback.message.answer(f"⭐ Оценка: {rating}/5\n\nНапишите короткий отзыв. Если текст не нужен — отправьте один символ «-».")
        await callback.answer()

    @router.message(AdvertisingSettlementState.waiting_review_text, F.chat.type == "private")
    async def review_save(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None: return
        data = await state.get_data(); deal_id = data.get("settlement_deal_id"); rating = data.get("settlement_rating")
        if not isinstance(deal_id, int) or not isinstance(rating, int): await state.clear(); return
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id))).scalar_one_or_none()
                if deal is None or message.from_user.id not in {deal.buyer_user_id, deal.seller_user_id} or deal.finished_at is None:
                    await state.clear(); await message.answer("Отзыв недоступен."); return
                exists = (await session.execute(select(AdvertisingReview.id).where(AdvertisingReview.deal_id == deal.id, AdvertisingReview.reviewer_user_id == message.from_user.id))).scalar_one_or_none()
                if exists is not None:
                    await state.clear(); await message.answer("Вы уже оставили отзыв."); return
                reviewed_id = deal.seller_user_id if message.from_user.id == deal.buyer_user_id else deal.buyer_user_id
                text = None if message.text.strip() == "-" else message.text[:2000]
                session.add(AdvertisingReview(deal_id=deal.id, reviewer_user_id=message.from_user.id, reviewed_user_id=reviewed_id, rating=rating, text=text))
        await state.clear()
        await message.answer(f"⭐ Спасибо. Ваша оценка <b>{rating}/5</b> сохранена.", parse_mode="HTML")

    return router
