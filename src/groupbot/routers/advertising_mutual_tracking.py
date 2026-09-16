from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualOp, AdvertisingManualOpCredit
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection, AdvertisingMutualOpMember
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.users import upsert_user


def _is_member(status: str, member) -> bool:
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and getattr(member, "is_member", True)
    )


async def _track_manual_leave(session: AsyncSession, *, target_chat_id: int, user_id: int, new_status: str) -> None:
    credits = list((await session.execute(
        select(AdvertisingManualOpCredit, AdvertisingManualOp)
        .join(AdvertisingManualOp, AdvertisingManualOp.id == AdvertisingManualOpCredit.op_id)
        .where(AdvertisingManualOp.target_chat_id == target_chat_id, AdvertisingManualOp.status == "active", AdvertisingManualOpCredit.user_id == user_id)
        .with_for_update()
    )).all())
    for credit, op in credits:
        if new_status in {"kicked", "banned"}:
            credit.satisfied = True
            credit.reason = "restricted"
            continue
        if new_status != "left" or credit.reason not in {"joined", "join_request"}:
            continue
        if credit.counted and op.mode == "subscribers":
            op.progress_count = max(op.progress_count - 1, 0)
        credit.counted = False
        credit.satisfied = False
        credit.reason = "left"


def create_advertising_mutual_tracking_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_mutual_tracking")

    @router.callback_query(F.data == "ads:manual")
    async def manual_private_entry(callback: CallbackQuery) -> None:
        async with session_factory() as session:
            rows = (await session.execute(
                select(Group.chat_id, Group.title).join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                .where(GroupOwner.user_id == callback.from_user.id, GroupOwner.is_current.is_(True), Group.status == GroupStatus.active.value)
                .order_by(Group.title, Group.chat_id)
            )).all()
        buttons = [[InlineKeyboardButton(text=(title or "Группа")[:60], callback_data=f"ads:manual:source:{chat_id}")] for chat_id, title in rows]
        if callback.message is not None:
            await callback.message.edit_text("Выберите группу, в которой нужно включить ОП:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)
        await callback.answer()

    @router.chat_join_request()
    async def track_join_request(event: ChatJoinRequest) -> None:
        if event.from_user.is_bot or event.invite_link is None:
            return
        invite = event.invite_link.invite_link
        async with session_factory() as session:
            async with session.begin():
                await upsert_user(session, event.from_user)
                from groupbot.routers.advertising_manual_op import _bind_and_credit
                await _bind_and_credit(session, invite_url=invite, target_chat_id=event.chat.id, target_title=event.chat.title or "Рекламная группа", user_id=event.from_user.id, reason="join_request")

    @router.chat_member()
    async def track(event: ChatMemberUpdated) -> None:
        user = event.new_chat_member.user
        if user.is_bot:
            return
        old_is_member = _is_member(event.old_chat_member.status, event.old_chat_member)
        new_is_member = _is_member(event.new_chat_member.status, event.new_chat_member)
        if old_is_member == new_is_member:
            return
        async with session_factory() as session:
            async with session.begin():
                await upsert_user(session, user)
                if new_is_member:
                    from groupbot.routers.advertising_manual_op import _bind_and_credit
                    invite = event.invite_link.invite_link if event.invite_link is not None else None
                    await _bind_and_credit(session, invite_url=invite, target_chat_id=event.chat.id, target_title=event.chat.title or "Рекламная группа", user_id=user.id, reason="joined")
                    previous = list((await session.execute(
                        select(AdvertisingMutualOpMember).join(AdvertisingMutualOpDirection, AdvertisingMutualOpDirection.id == AdvertisingMutualOpMember.direction_id)
                        .where(AdvertisingMutualOpDirection.target_chat_id == event.chat.id, AdvertisingMutualOpDirection.status == "active", AdvertisingMutualOpMember.user_id == user.id, AdvertisingMutualOpMember.is_active.is_(False)).with_for_update()
                    )).scalars().all())
                    if previous:
                        for member in previous:
                            member.is_active = True
                            member.left_at = None
                        return
                    if not invite:
                        return
                    direction = (await session.execute(select(AdvertisingMutualOpDirection).where(AdvertisingMutualOpDirection.target_chat_id == event.chat.id, AdvertisingMutualOpDirection.status == "active", AdvertisingMutualOpDirection.invite_link == invite).limit(1))).scalar_one_or_none()
                    if direction is None:
                        return
                    await session.execute(insert(AdvertisingMutualOpMember).values(direction_id=direction.id, user_id=user.id, is_active=True, left_at=None).on_conflict_do_update(constraint="uq_mutual_op_direction_user", set_={"is_active": True, "left_at": None}))
                else:
                    await _track_manual_leave(session, target_chat_id=event.chat.id, user_id=user.id, new_status=event.new_chat_member.status)
                    rows = list((await session.execute(
                        select(AdvertisingMutualOpMember).join(AdvertisingMutualOpDirection, AdvertisingMutualOpDirection.id == AdvertisingMutualOpMember.direction_id)
                        .where(AdvertisingMutualOpDirection.target_chat_id == event.chat.id, AdvertisingMutualOpDirection.status == "active", AdvertisingMutualOpMember.user_id == user.id, AdvertisingMutualOpMember.is_active.is_(True)).with_for_update()
                    )).scalars().all())
                    for member in rows:
                        member.is_active = False
                        member.left_at = event.date

    from groupbot.routers.advertising_manual_op import create_advertising_manual_op_router
    router.include_router(create_advertising_manual_op_router(session_factory))
    return router
