from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupMember, MemberStatus
from groupbot.moderation_models import ObservedMessage
from groupbot.services.users import upsert_user


def normalize_message_text(message: Message) -> str | None:
    raw = message.text or message.caption
    if not raw:
        return None
    value = re.sub(r"\s+", " ", raw.casefold()).strip()
    return value[:4000] or None


class GroupMemberTrackingMiddleware(BaseMiddleware):
    """Keep users/group_members and observed messages from real group activity."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _touch_member(self, session: AsyncSession, chat_id: int, telegram_user) -> None:
        if telegram_user is None or telegram_user.is_bot:
            return
        await upsert_user(session, telegram_user)
        await session.execute(
            insert(GroupMember)
            .values(
                chat_id=chat_id,
                user_id=telegram_user.id,
                status=MemberStatus.member.value,
                joined_at=func.now(),
                last_activity_at=func.now(),
            )
            .on_conflict_do_update(
                constraint="uq_group_member_chat_user",
                set_={
                    "status": MemberStatus.member.value,
                    "left_at": None,
                    "last_activity_at": func.now(),
                },
            )
        )

    async def _remember_message(self, session: AsyncSession, event: Message) -> None:
        user = event.from_user
        if user is None or user.is_bot:
            return
        await session.execute(
            insert(ObservedMessage)
            .values(
                chat_id=event.chat.id,
                message_id=event.message_id,
                user_id=user.id,
                sent_at=event.date,
                normalized_text=normalize_message_text(event),
            )
            .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)

        async with self.session_factory() as session:
            known_group = (
                await session.execute(select(Group.chat_id).where(Group.chat_id == event.chat.id))
            ).scalar_one_or_none()
            if known_group is not None:
                async with session.begin_nested():
                    await self._touch_member(session, event.chat.id, event.from_user)
                    await self._remember_message(session, event)
                    for new_user in event.new_chat_members or []:
                        await self._touch_member(session, event.chat.id, new_user)

                    if event.left_chat_member is not None and not event.left_chat_member.is_bot:
                        await upsert_user(session, event.left_chat_member)
                        await session.execute(
                            insert(GroupMember)
                            .values(
                                chat_id=event.chat.id,
                                user_id=event.left_chat_member.id,
                                status=MemberStatus.left.value,
                                joined_at=func.now(),
                                left_at=func.now(),
                                last_activity_at=func.now(),
                            )
                            .on_conflict_do_update(
                                constraint="uq_group_member_chat_user",
                                set_={
                                    "status": MemberStatus.left.value,
                                    "left_at": func.now(),
                                },
                            )
                        )
                await session.commit()

        return await handler(event, data)
