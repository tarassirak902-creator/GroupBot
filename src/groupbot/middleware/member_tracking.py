from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, Group, GroupMember, MemberStatus
from groupbot.moderation_models import ObservedMessage
from groupbot.services.audit import write_audit
from groupbot.services.users import upsert_user
from groupbot.telegram_admin_models import TelegramAdminPromotion


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

    async def _cleanup_departed_member(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
        status: str,
    ) -> None:
        """Fallback cleanup when only a left_chat_member service message arrives."""
        # Lazy imports avoid pulling rank-router policy modules into middleware
        # import time while keeping the runtime cleanup identical to chat_member.
        from groupbot.services.helper_role_policy import HELPER_ROLE, detach_helpers_from_mentor
        from groupbot.services.special_statuses import remove_special_statuses_for_user

        assignment = (
            await session.execute(
                select(AdminAssignment)
                .where(
                    AdminAssignment.chat_id == chat_id,
                    AdminAssignment.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        role = None
        was_reserve = False
        if assignment is not None:
            was_reserve = bool(assignment.is_reserve)
            if assignment.role_id is not None:
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
                ).scalar_one_or_none()
            if role is not None and role.name != HELPER_ROLE:
                await detach_helpers_from_mentor(
                    session,
                    chat_id=chat_id,
                    mentor_id=user_id,
                    actor_id=None,
                    reason=f"mentor_{status}_service_fallback",
                )
            await session.delete(assignment)

        promotion = (
            await session.execute(
                select(TelegramAdminPromotion)
                .where(
                    TelegramAdminPromotion.chat_id == chat_id,
                    TelegramAdminPromotion.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if promotion is not None:
            await session.delete(promotion)

        removed_statuses = await remove_special_statuses_for_user(
            session,
            chat_id=chat_id,
            user_id=user_id,
        )
        if assignment is not None or promotion is not None or removed_statuses:
            await write_audit(
                session,
                "group.member_exit_service_cleanup",
                chat_id=chat_id,
                actor_user_id=None,
                target_type="user",
                target_id=str(user_id),
                payload={
                    "member_status": status,
                    "role_name": role.name if role is not None else None,
                    "was_reserve": was_reserve,
                    "telegram_promotion_tracking_removed": promotion is not None,
                    "special_statuses_removed": removed_statuses,
                },
            )

    async def _mark_left_from_service_message(self, session: AsyncSession, chat_id: int, telegram_user) -> None:
        """Treat left_chat_member as fallback without overwriting a known ban."""
        await upsert_user(session, telegram_user)
        current_status = (
            await session.execute(
                select(GroupMember.status).where(
                    GroupMember.chat_id == chat_id,
                    GroupMember.user_id == telegram_user.id,
                )
            )
        ).scalar_one_or_none()
        target_status = (
            MemberStatus.banned.value
            if current_status == MemberStatus.banned.value
            else MemberStatus.left.value
        )
        await session.execute(
            insert(GroupMember)
            .values(
                chat_id=chat_id,
                user_id=telegram_user.id,
                status=target_status,
                joined_at=func.now(),
                left_at=func.now(),
                last_activity_at=func.now(),
            )
            .on_conflict_do_update(
                constraint="uq_group_member_chat_user",
                set_={
                    "status": target_status,
                    "left_at": func.now(),
                },
            )
        )
        await self._cleanup_departed_member(
            session,
            chat_id=chat_id,
            user_id=telegram_user.id,
            status=target_status,
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
                        await self._mark_left_from_service_message(
                            session,
                            event.chat.id,
                            event.left_chat_member,
                        )
                await session.commit()

        return await handler(event, data)
