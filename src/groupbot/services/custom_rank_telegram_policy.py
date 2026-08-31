from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminPermission, AdminRole
from groupbot.routers import admin_member_sync as _member_sync_module
from groupbot.routers import admin_punishment_lists as _punishment_lists_module
from groupbot.routers import admin_rank_audit_actions as _audit_actions_module
from groupbot.routers import admin_rank_compact_actions as _compact_actions_module
from groupbot.routers import admin_rank_target_actions as _target_actions_module
from groupbot.routers import group_control_role_actions as _role_actions_module
from groupbot.routers import group_control_ux as _role_ux_module
from groupbot.routers import manual_moderation as _manual_moderation_module
from groupbot.routers.admin_hierarchy import STANDARD_NAMES
from groupbot.routers.group_control import KNOWN_PERMISSIONS
from groupbot.services import helper_role_policy as _helper_role_policy
from groupbot.services.helper_role_policy import NO_ADMIN_RIGHTS
from groupbot.services.permissions import has_permission
from groupbot.telegram_admin_models import TelegramAdminPromotion


KNOWN_PERMISSIONS[:] = [
    (key, title) for key, title in KNOWN_PERMISSIONS if key != "stats"
]

_helper_role_policy.STANDARD_PERMISSION_KEYS.discard("stats")
_helper_role_policy.STANDARD_PERMISSION_KEYS.add("punishment_lists")
for _role_name in (
    _helper_role_policy.DEPUTY_ROLE,
    _helper_role_policy.CHIEF_ROLE,
    _helper_role_policy.CHAT_ADMIN_ROLE,
):
    _helper_role_policy.STANDARD_ROLE_DEFAULT_PERMISSIONS[_role_name].discard("stats")
    _helper_role_policy.STANDARD_ROLE_DEFAULT_PERMISSIONS[_role_name].add("punishment_lists")
_helper_role_policy.STANDARD_ROLE_DEFAULT_PERMISSIONS[
    _helper_role_policy.VOICE_ADMIN_ROLE
].discard("stats")
_helper_role_policy.STANDARD_ROLE_DEFAULT_PERMISSIONS[
    _helper_role_policy.HELPER_ROLE
].discard("stats")

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
_STANDARD_ROLE_IDS: set[int] = set()


async def punishment_list_access(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
) -> bool:
    return await has_permission(session, chat_id, user_id, "punishment_lists")


def role_editor_keyboard(
    chat_id: int,
    role_id: int,
    permissions: dict[str, bool],
    *,
    role_active: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, title in KNOWN_PERMISSIONS:
        rows.append([
            InlineKeyboardButton(
                text=f"{'✅' if permissions.get(key, False) else '❌'} {title}",
                callback_data=f"gctl:perm:{chat_id}:{role_id}:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"gctl:perm_save:{chat_id}:{role_id}")])
    rows.append([
        InlineKeyboardButton(
            text="⛔ Выключить ранг" if role_active else "✅ Включить ранг",
            callback_data=f"gctl:role_toggle:{chat_id}:{role_id}",
        )
    ])
    if role_id not in _STANDARD_ROLE_IDS:
        rows.append([InlineKeyboardButton(text="🗑 Удалить ранг", callback_data=f"gctl:role_delete:{chat_id}:{role_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_role_editor(
    target: Message,
    *,
    chat_id: int,
    role: AdminRole,
    assignments: int,
    permissions: dict[str, bool],
) -> None:
    if role.name in STANDARD_NAMES:
        _STANDARD_ROLE_IDS.add(role.id)
    else:
        _STANDARD_ROLE_IDS.discard(role.id)
    await target.edit_text(
        "👑 <b>Настройка админ-ранга</b>\n\n"
        f"Название: <b>{escape(role.name)}</b>\n"
        f"Статус: {'✅ включён' if role.is_active else '⛔ выключен'}\n"
        f"Назначено пользователей: <b>{assignments}</b>\n\n"
        "Выберите нужные разрешения. Изменения применятся только после нажатия <b>💾 Сохранить</b>.",
        parse_mode="HTML",
        reply_markup=role_editor_keyboard(
            chat_id,
            role.id,
            permissions,
            role_active=role.is_active,
        ),
    )


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
    # The Telegram side effect must be serialized with role toggle/delete. Both
    # those paths lock AdminRole FOR UPDATE. Keeping this lock until the caller's
    # assignment transaction commits prevents a stale button from promoting a
    # user after the role was disabled or removed.
    locked_role = (
        await session.execute(
            select(AdminRole)
            .where(
                AdminRole.id == role.id,
                AdminRole.chat_id == chat_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_role is None:
        return "Ранг больше недоступен. Откройте список рангов заново."
    if not locked_role.is_active:
        return "Ранг выключен. Включите его перед назначением."

    if locked_role.name in STANDARD_NAMES:
        return await _original_ensure_telegram_admin_for_role(
            bot,
            session,
            chat_id=chat_id,
            target_id=target_id,
            role=locked_role,
            telegram_member=telegram_member,
        )

    if not await _custom_role_needs_telegram_admin(session, role_id=locked_role.id):
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
        role=locked_role,
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
    return await sync_custom_role_permissions(
        callback,
        session,
        chat_id=chat_id,
        role_id=role_id,
    )


for _rank_module in (
    _member_sync_module,
    _audit_actions_module,
    _compact_actions_module,
    _target_actions_module,
):
    _rank_module._ensure_telegram_admin_for_role = ensure_telegram_admin_for_custom_role

_role_actions_module._sync_managed_telegram_admins_for_role = sync_custom_role_permissions
_role_ux_module._sync_managed_telegram_admins_for_role = sync_custom_role_permissions
_role_actions_module._sync_managed_telegram_admins_for_role_state = sync_custom_role_state
_role_ux_module._sync_managed_telegram_admins_for_role_state = sync_custom_role_state
_role_ux_module._permission_editor_keyboard = role_editor_keyboard
_role_ux_module._render_permission_editor = render_role_editor
_manual_moderation_module._admin_access = punishment_list_access
_punishment_lists_module._admin_access = punishment_list_access
