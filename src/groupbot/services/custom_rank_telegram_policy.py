from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminPermission, AdminRole
from groupbot.routers import admin_member_sync as _member_sync_module
from groupbot.routers import group_control_role_actions as _role_actions_module
from groupbot.routers import group_control_ux as _role_ux_module
from groupbot.routers.admin_hierarchy import STANDARD_NAMES
from groupbot.routers.group_control import KNOWN_PERMISSIONS
from groupbot.services.helper_role_policy import NO_ADMIN_RIGHTS
from groupbot.telegram_admin_models import TelegramAdminPromotion


# Full/common group statistics are deliberately rank-based, not a configurable
# AdminPermission. Remove the historical switch from the shared editor catalog
# so Voice Admin/custom roles cannot be shown a setting that enforcement ignores.
KNOWN_PERMISSIONS[:] = [
    (key, title) for key, title in KNOWN_PERMISSIONS if key != "stats"
]

CUSTOM_TELEGRAM_PERMISSIONS = {
    "mute",
    "ban",
    "unmute",
    "unban",
    "delete",
    "pin",
}

_original_ensure_telegram_admin_for_role = _member_sync_module._ensure_telegram_admin_for_role
_original_sync_role_permissions = _role_actions_module._sync_managed_telegram_admins_for_role
_original_sync_role_state = _role_actions_module._sync_managed_telegram_admins_for_role_state


async def _custom_role_needs_telegram_admin(
    session: AsyncSession,
    *,
    role_id: int,
) -> bool:
    allowed = set((
        await session.execute(
            select(AdminPermission.permission).where(
                AdminPermission.role_id == role_id,
                AdminPermission.allowed.is_(True),
            )
        )
    ).scalars().all())
    return bool(allowed & CUSTOM_TELEGRAM_PERMISSIONS)


async def _tracked_promotion(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> TelegramAdminPromotion | None:
    return (
        await session.execute(
            select(TelegramAdminPromotion)
            .where(
                TelegramAdminPromotion.chat_id == chat_id,
                TelegramAdminPromotion.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _demote_tracked_custom_admin(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    member,
) -> str | None:
    promotion = await _tracked_promotion(
        session,
        chat_id=chat_id,
        user_id=user_id,
    )
    if promotion is None:
        return None

    if getattr(member.status, "value", str(member.status)) == "administrator":
        error = await _member_sync_module._check_bot_promotion_rights(bot, chat_id, {})
        if error:
            return error
        try:
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                is_anonymous=False,
                **NO_ADMIN_RIGHTS,
            )
        except Exception:
            return "Telegram не позволил снять лишние права пользовательского ранга."

    await session.delete(promotion)
    return None


async def ensure_telegram_admin_for_custom_role(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    telegram_member,
) -> str | None:
    if role.name in STANDARD_NAMES:
        return await _original_ensure_telegram_admin_for_role(
            bot,
            session,
            chat_id=chat_id,
            target_id=target_id,
            role=role,
            telegram_member=telegram_member,
        )

    if not await _custom_role_needs_telegram_admin(session, role_id=role.id):
        # A custom Mimorus rank with no Telegram-relevant permissions must not
        # silently turn an ordinary participant into a Telegram administrator.
        return await _demote_tracked_custom_admin(
            bot,
            session,
            chat_id=chat_id,
            user_id=target_id,
            member=telegram_member,
        )

    return await _original_ensure_telegram_admin_for_role(
        bot,
        session,
        chat_id=chat_id,
        target_id=target_id,
        role=role,
        telegram_member=telegram_member,
    )


async def sync_custom_role_permissions(
    callback,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
) -> str | None:
    role = (
        await session.execute(
            select(AdminRole).where(
                AdminRole.id == role_id,
                AdminRole.chat_id == chat_id,
            )
        )
    ).scalar_one_or_none()
    if role is None or role.name in STANDARD_NAMES:
        return await _original_sync_role_permissions(
            callback,
            session,
            chat_id=chat_id,
            role_id=role_id,
        )

    target_ids = list((
        await session.execute(
            select(AdminAssignment.user_id).where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.role_id == role_id,
            )
        )
    ).scalars().all())
    if not target_ids:
        return None

    needs_admin = await _custom_role_needs_telegram_admin(session, role_id=role_id)
    rights = await _member_sync_module._telegram_rights_for_role(session, role_id)
    if needs_admin:
        error = await _member_sync_module._check_bot_promotion_rights(
            callback.bot,
            chat_id,
            rights,
        )
        if error:
            return error

    for target_id in target_ids:
        try:
            member = await callback.bot.get_chat_member(chat_id, int(target_id))
        except Exception:
            return f"Не удалось проверить пользователя Telegram ID {target_id}."

        status = getattr(member.status, "value", str(member.status))
        promotion = await _tracked_promotion(
            session,
            chat_id=chat_id,
            user_id=int(target_id),
        )

        if not needs_admin:
            error = await _demote_tracked_custom_admin(
                callback.bot,
                session,
                chat_id=chat_id,
                user_id=int(target_id),
                member=member,
            )
            if error:
                return error
            continue

        # Never overwrite a Telegram administrator that the owner appointed
        # manually outside Mimorus.
        if status == "administrator" and promotion is None:
            continue
        if status not in {"member", "restricted", "administrator"}:
            continue

        try:
            await callback.bot.promote_chat_member(
                chat_id=chat_id,
                user_id=int(target_id),
                is_anonymous=False,
                **rights,
            )
        except Exception:
            return "Telegram не позволил синхронизировать права пользовательского ранга."
        if promotion is None:
            session.add(
                TelegramAdminPromotion(chat_id=chat_id, user_id=int(target_id))
            )

    return None


async def sync_custom_role_state(
    callback,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
    enabled: bool,
) -> str | None:
    error = await _original_sync_role_state(
        callback,
        session,
        chat_id=chat_id,
        role_id=role_id,
        enabled=enabled,
    )
    if error is not None or not enabled:
        return error

    role = (
        await session.execute(
            select(AdminRole).where(
                AdminRole.id == role_id,
                AdminRole.chat_id == chat_id,
            )
        )
    ).scalar_one_or_none()
    if role is None or role.name in STANDARD_NAMES:
        return None

    # A custom rank may have been disabled while its permissions were edited.
    # Re-enabling must apply the currently saved Telegram-relevant permissions
    # even when no tracked promotion row existed before.
    return await sync_custom_role_permissions(
        callback,
        session,
        chat_id=chat_id,
        role_id=role_id,
    )


_member_sync_module._ensure_telegram_admin_for_role = ensure_telegram_admin_for_custom_role
_role_actions_module._sync_managed_telegram_admins_for_role = sync_custom_role_permissions
_role_ux_module._sync_managed_telegram_admins_for_role = sync_custom_role_permissions
_role_actions_module._sync_managed_telegram_admins_for_role_state = sync_custom_role_state
_role_ux_module._sync_managed_telegram_admins_for_role_state = sync_custom_role_state
