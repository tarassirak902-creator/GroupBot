from __future__ import annotations

from aiogram import F, Router
from aiogram.types import ChatMemberUpdated
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, Group, GroupMember, MemberStatus
from groupbot.services.audit import write_audit
from groupbot.services.helper_role_policy import HELPER_ROLE, detach_helpers_from_mentor
from groupbot.services.users import upsert_user
from groupbot.telegram_admin_models import TelegramAdminPromotion


_PRESENT_STATUSES = {"creator", "administrator", "member"}


def _status_value(member) -> str:
    return getattr(member.status, "value", str(member.status))


def _member_status(member) -> str:
    status = _status_value(member)
    if status == "kicked":
        return MemberStatus.banned.value
    if status == "left":
        return MemberStatus.left.value
    if status == "restricted" and not bool(getattr(member, "is_member", True)):
        return MemberStatus.left.value
    return MemberStatus.member.value


async def _store_member_status(
    session: AsyncSession,
    *,
    chat_id: int,
    user,
    status: str,
) -> None:
    values = {
        "chat_id": chat_id,
        "user_id": user.id,
        "status": status,
        "joined_at": func.now(),
        "last_activity_at": func.now(),
    }
    update_values: dict = {"status": status}
    if status == MemberStatus.member.value:
        update_values.update({
            "left_at": None,
            "joined_at": func.now(),
            "last_activity_at": func.now(),
        })
    else:
        values["left_at"] = func.now()
        update_values["left_at"] = func.now()

    await session.execute(
        insert(GroupMember)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_group_member_chat_user",
            set_=update_values,
        )
    )


async def _drop_stale_assignment(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    status: str,
) -> None:
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
    if assignment is None:
        return

    role = None
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
            reason=f"mentor_{status}",
        )

    if assignment.is_reserve:
        assignment.role_id = None
        assignment.assigned_by_user_id = None
    else:
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

    await write_audit(
        session,
        "group.admin_assignment_removed_on_member_exit",
        chat_id=chat_id,
        actor_user_id=None,
        target_type="user",
        target_id=str(user_id),
        payload={
            "member_status": status,
            "role_name": role.name if role is not None else None,
            "was_reserve": bool(assignment.is_reserve),
        },
    )


def create_member_status_sync_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="member_status_sync")

    @router.chat_member()
    async def member_status_changed(event: ChatMemberUpdated) -> None:
        if event.chat.type not in {"group", "supergroup"}:
            return
        user = event.new_chat_member.user
        if user.is_bot:
            return

        old_status = _member_status(event.old_chat_member)
        new_status = _member_status(event.new_chat_member)
        raw_old = _status_value(event.old_chat_member)
        raw_new = _status_value(event.new_chat_member)
        if old_status == new_status and raw_old == raw_new:
            return

        async with session_factory() as session:
            known_group = (
                await session.execute(select(Group.chat_id).where(Group.chat_id == event.chat.id))
            ).scalar_one_or_none()
            if known_group is None:
                return

            async with session.begin():
                await upsert_user(session, user)
                await _store_member_status(
                    session,
                    chat_id=event.chat.id,
                    user=user,
                    status=new_status,
                )
                if new_status in {MemberStatus.left.value, MemberStatus.banned.value}:
                    await _drop_stale_assignment(
                        session,
                        chat_id=event.chat.id,
                        user_id=user.id,
                        status=new_status,
                    )

                await write_audit(
                    session,
                    "group.member_status_changed",
                    chat_id=event.chat.id,
                    actor_user_id=(event.from_user.id if event.from_user else None),
                    target_type="user",
                    target_id=str(user.id),
                    payload={
                        "old_telegram_status": raw_old,
                        "new_telegram_status": raw_new,
                        "old_member_status": old_status,
                        "new_member_status": new_status,
                    },
                )

    return router
