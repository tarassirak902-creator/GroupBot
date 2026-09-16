from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ChatMemberUpdated
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualOp, AdvertisingManualOpCredit
from groupbot.models import AdminAssignment, AdminRole, Group, GroupMember, MemberStatus
from groupbot.services.audit import write_audit
from groupbot.services.helper_role_policy import HELPER_ROLE, detach_helpers_from_mentor
from groupbot.services.special_statuses import remove_special_statuses_for_user
from groupbot.services.users import upsert_user
from groupbot.telegram_admin_models import TelegramAdminPromotion

logger = logging.getLogger(__name__)


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


async def _store_member_status(session: AsyncSession, *, chat_id: int, user, status: str, rejoined: bool) -> None:
    values = {"chat_id": chat_id, "user_id": user.id, "status": status, "joined_at": func.now(), "last_activity_at": func.now()}
    update_values: dict = {"status": status}
    if status == MemberStatus.member.value:
        update_values["left_at"] = None
        if rejoined:
            update_values.update({"joined_at": func.now(), "last_activity_at": func.now()})
    else:
        values["left_at"] = func.now()
        update_values["left_at"] = func.now()
    await session.execute(insert(GroupMember).values(**values).on_conflict_do_update(constraint="uq_group_member_chat_user", set_=update_values))


async def _credit_manual_op_join(session: AsyncSession, *, chat_id: int, user_id: int, invite_url: str | None) -> None:
    if not invite_url:
        logger.info("MANUAL_OP_CREDIT_SKIP chat_id=%s user_id=%s reason=no_invite_url", chat_id, user_id)
        return
    ops = list((await session.execute(select(AdvertisingManualOp).where(
        AdvertisingManualOp.target_chat_id == chat_id,
        AdvertisingManualOp.target_url == invite_url,
        AdvertisingManualOp.status == "active",
        or_(AdvertisingManualOp.mode == "unlimited", AdvertisingManualOp.mode == "days", and_(AdvertisingManualOp.mode == "subscribers", AdvertisingManualOp.progress_count < AdvertisingManualOp.quantity)),
    ).with_for_update())).scalars().all())
    logger.info("MANUAL_OP_CREDIT_LOOKUP chat_id=%s user_id=%s invite_url=%r matched_ops=%s", chat_id, user_id, invite_url, [op.id for op in ops])
    for op in ops:
        credit = (await session.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id == op.id, AdvertisingManualOpCredit.user_id == user_id).with_for_update())).scalar_one_or_none()
        if credit is None:
            counted = op.mode == "subscribers"
            session.add(AdvertisingManualOpCredit(op_id=op.id, user_id=user_id, satisfied=True, counted=counted, reason="invite_link_join"))
            if counted:
                op.progress_count = min(op.progress_count + 1, op.quantity)
            logger.info("MANUAL_OP_CREDIT_ADD op_id=%s chat_id=%s user_id=%s counted=%s progress=%s quantity=%s", op.id, chat_id, user_id, counted, op.progress_count, op.quantity)
        else:
            credit.satisfied = True
            credit.reason = "invite_link_rejoin"
            added = False
            if op.mode == "subscribers" and not credit.counted and op.progress_count < op.quantity:
                credit.counted = True
                op.progress_count += 1
                added = True
            logger.info("MANUAL_OP_CREDIT_REJOIN op_id=%s chat_id=%s user_id=%s added=%s progress=%s quantity=%s", op.id, chat_id, user_id, added, op.progress_count, op.quantity)


async def _uncount_manual_op_leave(session: AsyncSession, *, chat_id: int, user_id: int) -> None:
    rows = list((await session.execute(
        select(AdvertisingManualOpCredit, AdvertisingManualOp)
        .join(AdvertisingManualOp, AdvertisingManualOp.id == AdvertisingManualOpCredit.op_id)
        .where(AdvertisingManualOp.target_chat_id == chat_id, AdvertisingManualOp.status == "active", AdvertisingManualOpCredit.user_id == user_id, AdvertisingManualOpCredit.counted.is_(True))
        .with_for_update()
    )).all())
    logger.info("MANUAL_OP_LEAVE_LOOKUP chat_id=%s user_id=%s matched_ops=%s", chat_id, user_id, [op.id for _, op in rows])
    for credit, op in rows:
        if op.mode == "subscribers":
            op.progress_count = max(op.progress_count - 1, 0)
        credit.counted = False
        credit.satisfied = False
        credit.reason = "left"
        logger.info("MANUAL_OP_CREDIT_REMOVE op_id=%s chat_id=%s user_id=%s progress=%s", op.id, chat_id, user_id, op.progress_count)


async def _drop_stale_assignment(session: AsyncSession, *, chat_id: int, user_id: int, status: str) -> None:
    assignment = (await session.execute(select(AdminAssignment).where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == user_id).with_for_update())).scalar_one_or_none()
    role = None
    was_reserve = False
    if assignment is not None:
        was_reserve = bool(assignment.is_reserve)
        if assignment.role_id is not None:
            role = (await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))).scalar_one_or_none()
        if role is not None and role.name != HELPER_ROLE:
            await detach_helpers_from_mentor(session, chat_id=chat_id, mentor_id=user_id, actor_id=None, reason=f"mentor_{status}")
        await session.delete(assignment)
    promotion = (await session.execute(select(TelegramAdminPromotion).where(TelegramAdminPromotion.chat_id == chat_id, TelegramAdminPromotion.user_id == user_id).with_for_update())).scalar_one_or_none()
    if promotion is not None:
        await session.delete(promotion)
    if assignment is not None or promotion is not None:
        await write_audit(session, "group.admin_assignment_removed_on_member_exit", chat_id=chat_id, actor_user_id=None, target_type="user", target_id=str(user_id), payload={"member_status": status, "role_name": role.name if role is not None else None, "was_reserve": was_reserve, "telegram_promotion_tracking_removed": promotion is not None})


async def _release_telegram_admin_state_after_manual_demotion(session: AsyncSession, *, chat_id: int, user_id: int, actor_id: int | None) -> tuple[bool, bool]:
    assignment = (await session.execute(select(AdminAssignment).where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == user_id).with_for_update())).scalar_one_or_none()
    reserve_cleared = False
    if assignment is not None and assignment.is_reserve:
        reserve_cleared = True
        if assignment.role_id is None:
            await session.delete(assignment)
        else:
            assignment.is_reserve = False
        await write_audit(session, "group.reserve_admin_cleared", chat_id=chat_id, actor_user_id=actor_id, target_type="user", target_id=str(user_id), payload={"reason": "telegram_admin_status_removed"})
    promotion = (await session.execute(select(TelegramAdminPromotion).where(TelegramAdminPromotion.chat_id == chat_id, TelegramAdminPromotion.user_id == user_id).with_for_update())).scalar_one_or_none()
    promotion_released = promotion is not None
    if promotion is not None:
        await session.delete(promotion)
        await write_audit(session, "group.telegram_admin_promotion_tracking_released", chat_id=chat_id, actor_user_id=actor_id, target_type="user", target_id=str(user_id), payload={"reason": "telegram_admin_status_removed"})
    return reserve_cleared, promotion_released


def create_member_status_sync_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
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
        invite_url = getattr(getattr(event, "invite_link", None), "invite_link", None)
        rejoined = old_status != MemberStatus.member.value and new_status == MemberStatus.member.value
        left = old_status == MemberStatus.member.value and new_status in {MemberStatus.left.value, MemberStatus.banned.value}
        logger.info("MANUAL_OP_MEMBER_EVENT chat_id=%s user_id=%s raw_old=%s raw_new=%s old=%s new=%s rejoined=%s left=%s invite_present=%s invite_url=%r", event.chat.id, user.id, raw_old, raw_new, old_status, new_status, rejoined, left, bool(invite_url), invite_url)
        if old_status == new_status and raw_old == raw_new:
            return
        async with session_factory() as session:
            async with session.begin():
                known_group = (await session.execute(select(Group.chat_id).where(Group.chat_id == event.chat.id))).scalar_one_or_none()
                if known_group is None:
                    logger.info("MANUAL_OP_MEMBER_SKIP chat_id=%s user_id=%s reason=unknown_group", event.chat.id, user.id)
                    return
                await upsert_user(session, user)
                actor_id = None
                if event.from_user is not None and not event.from_user.is_bot:
                    await upsert_user(session, event.from_user)
                    actor_id = event.from_user.id
                await _store_member_status(session, chat_id=event.chat.id, user=user, status=new_status, rejoined=rejoined)
                if rejoined:
                    if invite_url:
                        await _credit_manual_op_join(session, chat_id=event.chat.id, user_id=user.id, invite_url=invite_url)
                    else:
                        logger.info("MANUAL_OP_CREDIT_SKIP chat_id=%s user_id=%s reason=rejoin_without_invite", event.chat.id, user.id)
                if left:
                    await _uncount_manual_op_leave(session, chat_id=event.chat.id, user_id=user.id)
                removed_special_statuses: list[str] = []
                reserve_cleared = False
                promotion_tracking_released = False
                if new_status in {MemberStatus.left.value, MemberStatus.banned.value}:
                    await _drop_stale_assignment(session, chat_id=event.chat.id, user_id=user.id, status=new_status)
                    removed_special_statuses = await remove_special_statuses_for_user(session, chat_id=event.chat.id, user_id=user.id)
                    if removed_special_statuses:
                        await write_audit(session, "group.special_statuses_removed_on_member_exit", chat_id=event.chat.id, actor_user_id=None, target_type="user", target_id=str(user.id), payload={"member_status": new_status, "statuses": removed_special_statuses})
                elif raw_old == "administrator" and raw_new != "administrator":
                    reserve_cleared, promotion_tracking_released = await _release_telegram_admin_state_after_manual_demotion(session, chat_id=event.chat.id, user_id=user.id, actor_id=actor_id)
                await write_audit(session, "group.member_status_changed", chat_id=event.chat.id, actor_user_id=actor_id, target_type="user", target_id=str(user.id), payload={"old_telegram_status": raw_old, "new_telegram_status": raw_new, "old_member_status": old_status, "new_member_status": new_status, "invite_link_present": bool(invite_url), "manual_op_credit_removed_on_leave": left, "special_statuses_removed": removed_special_statuses, "reserve_cleared": reserve_cleared, "telegram_promotion_tracking_released": promotion_tracking_released})

    return router
