from __future__ import annotations

from aiogram import Router
from aiogram.types import ChatMemberUpdated
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection, AdvertisingMutualOpMember
from groupbot.services.users import upsert_user


def _is_member(status: str, member) -> bool:
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and getattr(member, "is_member", True)
    )


def create_advertising_mutual_tracking_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_mutual_tracking")

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
                    # A previously credited user counts again after rejoining, even if
                    # the second join was not made through the advertising invite link.
                    previous = list((await session.execute(
                        select(AdvertisingMutualOpMember)
                        .join(AdvertisingMutualOpDirection, AdvertisingMutualOpDirection.id == AdvertisingMutualOpMember.direction_id)
                        .where(
                            AdvertisingMutualOpDirection.target_chat_id == event.chat.id,
                            AdvertisingMutualOpDirection.status == "active",
                            AdvertisingMutualOpMember.user_id == user.id,
                            AdvertisingMutualOpMember.is_active.is_(False),
                        )
                        .with_for_update()
                    )).scalars().all())
                    if previous:
                        for member in previous:
                            member.is_active = True
                            member.left_at = None
                        return

                    # First credit is strict: the user must join using the unique
                    # invite link created for this mutual OP direction.
                    invite = event.invite_link.invite_link if event.invite_link is not None else None
                    if not invite:
                        return
                    direction = (await session.execute(select(AdvertisingMutualOpDirection).where(
                        AdvertisingMutualOpDirection.target_chat_id == event.chat.id,
                        AdvertisingMutualOpDirection.status == "active",
                        AdvertisingMutualOpDirection.invite_link == invite,
                    ).limit(1))).scalar_one_or_none()
                    if direction is None:
                        return
                    await session.execute(
                        insert(AdvertisingMutualOpMember)
                        .values(direction_id=direction.id, user_id=user.id, is_active=True, left_at=None)
                        .on_conflict_do_update(
                            constraint="uq_mutual_op_direction_user",
                            set_={"is_active": True, "left_at": None},
                        )
                    )
                else:
                    rows = list((await session.execute(
                        select(AdvertisingMutualOpMember)
                        .join(AdvertisingMutualOpDirection, AdvertisingMutualOpDirection.id == AdvertisingMutualOpMember.direction_id)
                        .where(
                            AdvertisingMutualOpDirection.target_chat_id == event.chat.id,
                            AdvertisingMutualOpDirection.status == "active",
                            AdvertisingMutualOpMember.user_id == user.id,
                            AdvertisingMutualOpMember.is_active.is_(True),
                        )
                        .with_for_update()
                    )).scalars().all())
                    for member in rows:
                        member.is_active = False
                        member.left_at = event.date

    return router
