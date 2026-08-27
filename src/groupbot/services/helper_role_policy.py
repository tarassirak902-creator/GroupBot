from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole
from groupbot.services.audit import write_audit
from groupbot.telegram_admin_models import TelegramAdminPromotion

HELPER_ROLE = "Помощник"

NO_ADMIN_RIGHTS = {
    "can_manage_chat": False,
    "can_delete_messages": False,
    "can_manage_video_chats": False,
    "can_restrict_members": False,
    "can_promote_members": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_post_stories": False,
    "can_edit_stories": False,
    "can_delete_stories": False,
    "can_post_messages": False,
    "can_edit_messages": False,
    "can_pin_messages": False,
    "can_manage_topics": False,
}


async def prepare_helper_telegram_state(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    telegram_member,
) -> str | None:
    """Keep Helper as an ordinary Telegram member.

    Only Telegram admin status previously granted and tracked by Mimorus is removed.
    A Telegram administrator appointed manually by the group owner is never changed.
    """
    if role.name != HELPER_ROLE:
        return None

    promotion = (
        await session.execute(
            select(TelegramAdminPromotion)
            .where(
                TelegramAdminPromotion.chat_id == chat_id,
                TelegramAdminPromotion.user_id == target_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if promotion is None:
        return None

    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        return "Не удалось проверить права Mimorus в группе."

    if bot_member.status != "administrator" or not bool(getattr(bot_member, "can_promote_members", False)):
        return "Mimorus не может снять ранее выданную Telegram-админку: нет права назначать администраторов."

    if telegram_member.status == "administrator":
        try:
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                is_anonymous=False,
                **NO_ADMIN_RIGHTS,
            )
        except Exception:
            return "Telegram не позволил снять ранее выданную Mimorus админку перед назначением Помощника."

    await session.delete(promotion)
    return None


async def cleanup_helper_managed_admins(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
) -> str | None:
    """Ensure every user of the Helper role stays a normal Telegram member."""
    role = (
        await session.execute(
            select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    if role is None or role.name != HELPER_ROLE:
        return None

    target_ids = list((
        await session.execute(
            select(AdminAssignment.user_id)
            .join(
                TelegramAdminPromotion,
                (TelegramAdminPromotion.chat_id == AdminAssignment.chat_id)
                & (TelegramAdminPromotion.user_id == AdminAssignment.user_id),
            )
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.role_id == role_id,
            )
        )
    ).scalars().all())

    for target_id in target_ids:
        try:
            member = await bot.get_chat_member(chat_id, target_id)
        except Exception:
            return f"Не удалось проверить Помощника Telegram ID {target_id}."
        error = await prepare_helper_telegram_state(
            bot,
            session,
            chat_id=chat_id,
            target_id=target_id,
            role=role,
            telegram_member=member,
        )
        if error:
            return error
    return None


async def detach_helpers_from_mentor(
    session: AsyncSession,
    *,
    chat_id: int,
    mentor_id: int,
    actor_id: int | None,
    reason: str,
) -> int:
    """Remove Helper role from everyone attached to a mentor who stops being an admin."""
    helpers = list((
        await session.execute(
            select(AdminAssignment)
            .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.assigned_by_user_id == mentor_id,
                AdminRole.name == HELPER_ROLE,
            )
            .with_for_update()
        )
    ).scalars().all())

    detached = 0
    for helper in helpers:
        helper_id = helper.user_id
        if helper.is_reserve:
            helper.role_id = None
            helper.assigned_by_user_id = None
        else:
            await session.delete(helper)
        await write_audit(
            session,
            "group.helper_detached_from_mentor",
            chat_id=chat_id,
            actor_user_id=actor_id,
            target_type="user",
            target_id=str(helper_id),
            payload={"mentor_user_id": mentor_id, "reason": reason},
        )
        detached += 1
    return detached


async def remember_assignment_actor(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    actor_id: int,
) -> None:
    assignment = (
        await session.execute(
            select(AdminAssignment)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == target_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if assignment is None:
        return

    assignment.assigned_by_user_id = actor_id
    role = None
    if assignment.role_id is not None:
        role = (
            await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
        ).scalar_one_or_none()
    if role is not None and role.name == HELPER_ROLE:
        await detach_helpers_from_mentor(
            session,
            chat_id=chat_id,
            mentor_id=target_id,
            actor_id=actor_id,
            reason="mentor_became_helper",
        )
