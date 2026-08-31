from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingDispute, AdvertisingListing, AdvertisingReview


_STATUS_LABELS = {
    "pending": "⏳ Ожидает решения",
    "draft_post": "📝 Черновик поста",
    "draft_mandatory": "📝 Черновик ОП",
    "accepted": "🚀 Выполняется",
    "finished_waiting_confirmation": "⚖️ Нужна проверка сторон",
    "dispute_open": "⚠️ Открыт спор",
    "completed_mutual": "✅ Завершена сторонами",
    "completed_timeout": "✅ Завершена автоматически",
    "rejected": "❌ Отклонена",
    "cancelled": "🚫 Отменена",
}


def _kind(deal: AdvertisingDeal) -> str:
    if (deal.agreed_terms_json or {}).get("mutual_op"):
        return "🤝"
    if deal.requested_post and deal.requested_mandatory:
        return "📣+✅"
    if deal.requested_post:
        return "📣"
    return "✅"


def _keyboard(rows: list[tuple[AdvertisingDeal, AdvertisingListing]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for deal, listing in rows:
        status = _STATUS_LABELS.get(deal.status, deal.status)
        buttons.append([
            InlineKeyboardButton(
                text=f"{_kind(deal)} {listing.group_title_snapshot} · {status}"[:64],
                callback_data=f"ads:deal:{deal.id}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def create_advertising_reviews_nav_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_reviews_nav")

    @router.callback_query(F.data == "ads:reviews")
    async def reviews_and_disputes(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        async with session_factory() as session:
            rows = list((await session.execute(
                select(AdvertisingDeal, AdvertisingListing)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .where(
                    or_(
                        AdvertisingDeal.buyer_user_id == callback.from_user.id,
                        AdvertisingDeal.seller_user_id == callback.from_user.id,
                    )
                )
                .order_by(AdvertisingDeal.created_at.desc(), AdvertisingDeal.id.desc())
                .limit(50)
            )).all())
            open_disputes = int((await session.execute(
                select(AdvertisingDispute.id)
                .join(AdvertisingDeal, AdvertisingDeal.id == AdvertisingDispute.deal_id)
                .where(
                    AdvertisingDispute.status == "open",
                    or_(
                        AdvertisingDeal.buyer_user_id == callback.from_user.id,
                        AdvertisingDeal.seller_user_id == callback.from_user.id,
                    ),
                )
            )).scalars().unique().all().__len__())
            reviews_count = int((await session.execute(
                select(AdvertisingReview.id).where(
                    AdvertisingReview.reviewer_user_id == callback.from_user.id
                )
            )).scalars().all().__len__())

        if rows:
            text = (
                "⭐ <b>Отзывы и споры</b>\n\n"
                f"Открытых споров: <b>{open_disputes}</b>\n"
                f"Ваших отзывов: <b>{reviews_count}</b>\n\n"
                "Выберите сделку. Если размещение завершено и ещё не закрыто, внутри доступны «Претензий нет» и «Открыть спор». "
                "После завершения можно оставить отзыв."
            )
        else:
            text = (
                "⭐ <b>Отзывы и споры</b>\n\n"
                "У вас пока нет рекламных сделок. Отзывы и споры появляются после первой заявки."
            )
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_keyboard(rows),
        )
        await callback.answer()

    return router
