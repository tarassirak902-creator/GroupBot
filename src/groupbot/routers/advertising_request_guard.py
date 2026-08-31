from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingListing
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group


async def _listing_available(
    session: AsyncSession,
    *,
    listing_id: int,
    buyer_user_id: int,
) -> bool:
    row = (
        await session.execute(
            select(AdvertisingListing, GroupOwner.user_id)
            .join(Group, Group.chat_id == AdvertisingListing.chat_id)
            .join(
                GroupOwner,
                (GroupOwner.chat_id == Group.chat_id) & GroupOwner.is_current.is_(True),
            )
            .where(
                AdvertisingListing.id == listing_id,
                AdvertisingListing.is_active.is_(True),
                AdvertisingListing.owner_user_id != buyer_user_id,
                Group.status == GroupStatus.active.value,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return False
    listing, current_owner_id = row
    if int(current_owner_id) != listing.owner_user_id:
        return False
    return await active_subscription_for_group(session, listing.chat_id) is not None


def _listing_id(data: str) -> int | None:
    parts = data.split(":")
    try:
        if data.startswith("ads:request:") and len(parts) == 3:
            return int(parts[2])
        if data.startswith("ads:req:type:") and len(parts) == 5:
            return int(parts[3])
    except (TypeError, ValueError, IndexError):
        return None
    return None


def create_advertising_request_guard_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_request_guard")

    @router.callback_query(
        F.data.startswith("ads:request:") | F.data.startswith("ads:req:type:")
    )
    async def guard(callback: CallbackQuery) -> None:
        listing_id = _listing_id(callback.data or "")
        if listing_id is None:
            return
        async with session_factory() as session:
            available = await _listing_available(
                session,
                listing_id=listing_id,
                buyer_user_id=callback.from_user.id,
            )
        if available:
            # Returning without answering lets later advertising request routers
            # handle the callback normally.
            return
        await callback.answer(
            "Эта рекламная площадка сейчас недоступна: объявление выключено, группа отключена, владелец сменился или подписка закончилась.",
            show_alert=True,
        )

    return router
