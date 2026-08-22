from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import Chat
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import Group, GroupOwner, GroupSettings, GroupStatus
from groupbot.services.audit import write_audit
from groupbot.services.users import upsert_user


CONNECT_WINDOW = timedelta(minutes=1)
DISCONNECT_WINDOW = timedelta(minutes=2)


async def register_pending_group(session: AsyncSession, chat: Chat) -> Group:
    now = datetime.now(timezone.utc)
    await session.execute(
        insert(Group)
        .values(
            chat_id=chat.id,
            title=chat.title,
            status=GroupStatus.pending.value,
            bot_added_at=now,
            connect_deadline_at=now + CONNECT_WINDOW,
            connected_at=None,
            disabled_at=None,
            disconnect_deadline_at=None,
        )
        .on_conflict_do_update(
            index_elements=[Group.chat_id],
            set_={
                "title": chat.title,
                "status": GroupStatus.pending.value,
                "bot_added_at": now,
                "connect_deadline_at": now + CONNECT_WINDOW,
                "disabled_at": None,
                "disconnect_deadline_at": None,
            },
        )
    )
    await session.execute(
        insert(GroupSettings).values(chat_id=chat.id).on_conflict_do_nothing(index_elements=[GroupSettings.chat_id])
    )
    return (await session.execute(select(Group).where(Group.chat_id == chat.id))).scalar_one()


async def connect_group(session: AsyncSession, bot: Bot, chat_id: int, owner_user) -> Group:
    member = await bot.get_chat_member(chat_id, owner_user.id)
    if member.status != "creator":
        raise PermissionError("only_chat_owner")

    bot_me = await bot.get_me()
    bot_member = await bot.get_chat_member(chat_id, bot_me.id)
    if bot_member.status != "administrator":
        raise PermissionError("bot_not_admin")

    await upsert_user(session, owner_user)
    group = (await session.execute(select(Group).where(Group.chat_id == chat_id).with_for_update())).scalar_one_or_none()
    if group is None:
        chat = await bot.get_chat(chat_id)
        group = await register_pending_group(session, chat)

    now = datetime.now(timezone.utc)
    group.status = GroupStatus.active.value
    group.connected_at = now
    group.connect_deadline_at = None
    group.disabled_at = None
    group.disconnect_deadline_at = None

    existing_current = (await session.execute(
        select(GroupOwner).where(GroupOwner.chat_id == chat_id, GroupOwner.is_current.is_(True)).with_for_update()
    )).scalars().all()
    for row in existing_current:
        if row.user_id != owner_user.id:
            row.is_current = False
            row.revoked_at = now

    await session.execute(
        insert(GroupOwner)
        .values(chat_id=chat_id, user_id=owner_user.id, is_current=True, revoked_at=None)
        .on_conflict_do_update(
            constraint="uq_group_owner_chat_user",
            set_={"is_current": True, "revoked_at": None},
        )
    )
    await write_audit(
        session,
        "group.connected",
        chat_id=chat_id,
        actor_user_id=owner_user.id,
        target_type="group",
        target_id=str(chat_id),
    )
    return group


async def disable_group(session: AsyncSession, chat_id: int, actor_user_id: int) -> None:
    group = (await session.execute(select(Group).where(Group.chat_id == chat_id).with_for_update())).scalar_one()
    now = datetime.now(timezone.utc)
    group.status = GroupStatus.disabled.value
    group.disabled_at = now
    group.disconnect_deadline_at = now + DISCONNECT_WINDOW
    await write_audit(
        session,
        "group.disabled",
        chat_id=chat_id,
        actor_user_id=actor_user_id,
        target_type="group",
        target_id=str(chat_id),
    )
