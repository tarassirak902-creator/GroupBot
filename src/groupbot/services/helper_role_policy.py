from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminPermission, AdminRole, AuditLog
from groupbot.services.audit import write_audit
from groupbot.telegram_admin_models import TelegramAdminPromotion

HELPER_ROLE = "Помощник"
DEPUTY_ROLE = "Зам. владельца"
CHIEF_ROLE = "Глав. админ"
CHAT_ADMIN_ROLE = "Администратор чата"
VOICE_ADMIN_ROLE = "Администратор войса"

STANDARD_ROLE_DEFAULT_PERMISSIONS: dict[str, set[str]] = {
    DEPUTY_ROLE: {"warning", "mute", "ban", "unmute", "unban", "delete", "pin", "stats"},
    CHIEF_ROLE: {"warning", "mute", "ban", "unmute", "unban", "delete", "pin", "stats"},
    CHAT_ADMIN_ROLE: {"warning", "mute", "ban", "unmute", "unban", "delete", "pin"},
    VOICE_ADMIN_ROLE: set(),
    HELPER_ROLE: set(),
}
STANDARD_PERMISSION_KEYS = {
    "warning",
    "mute",
    "ban",
    "unmute",
    "unban",
    "delete",
    "pin",
    "stats",
}

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


def _standard_telegram_rights(role_name: str) -> dict[str, bool] | None:
    if role_name == HELPER_ROLE:
        return dict(NO_ADMIN_RIGHTS)
    if role_name == DEPUTY_ROLE:
        return {
            **NO_ADMIN_RIGHTS,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_manage_video_chats": True,
            "can_restrict_members": True,
            "can_invite_users": True,
            "can_pin_messages": True,
        }
    if role_name == CHIEF_ROLE:
        return {
            **NO_ADMIN_RIGHTS,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_restrict_members": True,
            "can_invite_users": True,
            "can_pin_messages": True,
        }
    if role_name == CHAT_ADMIN_ROLE:
        return {
            **NO_ADMIN_RIGHTS,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_restrict_members": True,
            "can_pin_messages": True,
        }
    if role_name == VOICE_ADMIN_ROLE:
        return {
            **NO_ADMIN_RIGHTS,
            "can_manage_chat": True,
            "can_manage_video_chats": True,
            "can_invite_users": True,
        }
    return None


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


# Install wrappers when this policy module is loaded by group_commands/helper routers.
# Rank routers have already been imported by main.py, so copied function references
# are replaced here as well to keep every assignment/removal path consistent.
from groupbot.routers import admin_hierarchy as _hierarchy_module  # noqa: E402
from groupbot.routers import admin_member_sync as _member_sync_module  # noqa: E402
from groupbot.routers import admin_rank_audit_actions as _audit_actions_module  # noqa: E402
from groupbot.routers import admin_rank_compact_actions as _compact_actions_module  # noqa: E402
from groupbot.routers import admin_rank_group_notifications as _group_notifications_module  # noqa: E402
from groupbot.routers import admin_rank_target_actions as _target_actions_module  # noqa: E402

_original_remove_assignment = _audit_actions_module._remove_assignment
_original_remove_role_and_managed_telegram_admin = _member_sync_module._remove_role_and_managed_telegram_admin
_original_ensure_standard_roles = _hierarchy_module._ensure_standard_roles
_original_telegram_rights_for_role = _member_sync_module._telegram_rights_for_role
_original_check_bot_promotion_rights = _member_sync_module._check_bot_promotion_rights
_original_ensure_telegram_admin_for_role = _member_sync_module._ensure_telegram_admin_for_role


async def _standard_role_was_customized(
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
) -> bool:
    row = (
        await session.execute(
            select(AuditLog.id)
            .where(
                AuditLog.chat_id == chat_id,
                AuditLog.event_type == "group.admin_permission_changed",
                AuditLog.target_type == "admin_role",
                AuditLog.target_id == str(role_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _apply_standard_permission_defaults(
    session: AsyncSession,
    *,
    chat_id: int,
    roles: list[AdminRole],
) -> None:
    for role in roles:
        defaults = STANDARD_ROLE_DEFAULT_PERMISSIONS.get(role.name)
        if defaults is None:
            continue
        if await _standard_role_was_customized(session, chat_id=chat_id, role_id=role.id):
            continue

        rows = list((
            await session.execute(
                select(AdminPermission)
                .where(AdminPermission.role_id == role.id)
                .with_for_update()
            )
        ).scalars().all())
        by_key = {row.permission: row for row in rows}
        for key in STANDARD_PERMISSION_KEYS:
            row = by_key.get(key)
            allowed = key in defaults
            if row is None:
                session.add(AdminPermission(role_id=role.id, permission=key, allowed=allowed))
            else:
                row.allowed = allowed


async def _ensure_standard_roles_with_defaults(session: AsyncSession, chat_id: int) -> list[AdminRole]:
    roles = await _original_ensure_standard_roles(session, chat_id)
    await _apply_standard_permission_defaults(session, chat_id=chat_id, roles=roles)
    return roles


async def _telegram_rights_for_role_with_standard_matrix(
    session: AsyncSession,
    role_id: int,
) -> dict[str, bool]:
    role = (
        await session.execute(select(AdminRole).where(AdminRole.id == role_id))
    ).scalar_one_or_none()
    if role is not None:
        rights = _standard_telegram_rights(role.name)
        if rights is not None:
            return rights
    return await _original_telegram_rights_for_role(session, role_id)


async def _check_bot_promotion_rights_with_standard_matrix(
    bot,
    chat_id: int,
    rights: dict[str, bool],
) -> str | None:
    error = await _original_check_bot_promotion_rights(bot, chat_id, rights)
    if error:
        return error
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        return "Не удалось проверить права Mimorus в группе."

    required = (
        ("can_manage_video_chats", "управление голосовыми чатами"),
        ("can_invite_users", "приглашение пользователей"),
    )
    for key, title in required:
        if rights.get(key) and not bool(getattr(member, key, False)):
            return f"Mimorus не может выдать право «{title}», потому что сам его не имеет."
    return None


async def _ensure_telegram_admin_for_role_with_helper_cleanup(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    telegram_member,
) -> str | None:
    if role.name == HELPER_ROLE:
        return await prepare_helper_telegram_state(
            bot,
            session,
            chat_id=chat_id,
            target_id=target_id,
            role=role,
            telegram_member=telegram_member,
        )
    return await _original_ensure_telegram_admin_for_role(
        bot,
        session,
        chat_id=chat_id,
        target_id=target_id,
        role=role,
        telegram_member=telegram_member,
    )


async def _remove_assignment_with_helper_cascade(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    assignment: AdminAssignment,
    role: AdminRole,
    actor_id: int,
):
    mentor_id = assignment.user_id
    result = await _original_remove_assignment(
        bot,
        session,
        chat_id=chat_id,
        assignment=assignment,
        role=role,
        actor_id=actor_id,
    )
    telegram_demoted, error = result
    if error is None and role.name != HELPER_ROLE:
        await detach_helpers_from_mentor(
            session,
            chat_id=chat_id,
            mentor_id=mentor_id,
            actor_id=actor_id,
            reason="mentor_removed_from_administration",
        )
    return telegram_demoted, error


async def _remove_role_with_helper_cascade(
    callback,
    session: AsyncSession,
    *,
    chat_id: int,
    assignment: AdminAssignment,
    role_id: int,
):
    mentor_id = assignment.user_id
    role = (
        await session.execute(select(AdminRole).where(AdminRole.id == role_id))
    ).scalar_one_or_none()
    error = await _original_remove_role_and_managed_telegram_admin(
        callback,
        session,
        chat_id=chat_id,
        assignment=assignment,
        role_id=role_id,
    )
    if error is None and role is not None and role.name != HELPER_ROLE:
        await detach_helpers_from_mentor(
            session,
            chat_id=chat_id,
            mentor_id=mentor_id,
            actor_id=callback.from_user.id,
            reason="mentor_removed_from_administration",
        )
    return error


_hierarchy_module._ensure_standard_roles = _ensure_standard_roles_with_defaults
_member_sync_module._ensure_standard_roles = _ensure_standard_roles_with_defaults
_compact_actions_module._ensure_standard_roles = _ensure_standard_roles_with_defaults

_member_sync_module._telegram_rights_for_role = _telegram_rights_for_role_with_standard_matrix
_member_sync_module._check_bot_promotion_rights = _check_bot_promotion_rights_with_standard_matrix
_compact_actions_module._telegram_rights_for_role = _telegram_rights_for_role_with_standard_matrix
_target_actions_module._telegram_rights_for_role = _telegram_rights_for_role_with_standard_matrix

_member_sync_module._ensure_telegram_admin_for_role = _ensure_telegram_admin_for_role_with_helper_cleanup
_audit_actions_module._ensure_telegram_admin_for_role = _ensure_telegram_admin_for_role_with_helper_cleanup
_compact_actions_module._ensure_telegram_admin_for_role = _ensure_telegram_admin_for_role_with_helper_cleanup
_target_actions_module._ensure_telegram_admin_for_role = _ensure_telegram_admin_for_role_with_helper_cleanup

_audit_actions_module._remove_assignment = _remove_assignment_with_helper_cascade
_compact_actions_module._remove_assignment = _remove_assignment_with_helper_cascade
_group_notifications_module._remove_assignment = _remove_assignment_with_helper_cascade
_member_sync_module._remove_role_and_managed_telegram_admin = _remove_role_with_helper_cascade
