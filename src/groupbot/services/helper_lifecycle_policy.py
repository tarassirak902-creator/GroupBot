from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole
from groupbot.routers import admin_member_sync as _member_sync_module
from groupbot.routers import admin_rank_audit_actions as _audit_actions_module
from groupbot.routers import admin_rank_compact_actions as _compact_actions_module
from groupbot.routers import admin_rank_target_actions as _target_actions_module
from groupbot.routers import group_control_role_actions as _role_actions_module
from groupbot.routers import group_control_ux as _role_ux_module
from groupbot.services.audit import write_audit
from groupbot.services.helper_role_policy import (
    CHAT_ADMIN_ROLE,
    CHIEF_ROLE,
    DEPUTY_ROLE,
    HELPER_ROLE,
    VOICE_ADMIN_ROLE,
    detach_helpers_from_mentor,
)
from groupbot.services.special_statuses import remove_special_statuses_for_user


MENTOR_ROLE_NAMES = {
    DEPUTY_ROLE,
    CHIEF_ROLE,
    CHAT_ADMIN_ROLE,
    VOICE_ADMIN_ROLE,
}

_original_assign_role = _member_sync_module._assign_role
_original_sync_role_state = _role_actions_module._sync_managed_telegram_admins_for_role_state


async def _current_role_name(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> str | None:
    return (
        await session.execute(
            select(AdminRole.name)
            .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == user_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def assign_role_with_mentor_lifecycle(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    actor_id: int,
) -> str | None:
    """Apply rank transition cleanup without breaking eligible mentor changes."""
    old_role_name = await _current_role_name(
        session,
        chat_id=chat_id,
        user_id=target_id,
    )
    error = await _original_assign_role(
        session,
        chat_id=chat_id,
        target_id=target_id,
        role=role,
        actor_id=actor_id,
    )
    if error is not None:
        return error

    if old_role_name in MENTOR_ROLE_NAMES and role.name not in MENTOR_ROLE_NAMES:
        await detach_helpers_from_mentor(
            session,
            chat_id=chat_id,
            mentor_id=target_id,
            actor_id=actor_id,
            reason="mentor_rank_changed_to_ineligible",
        )

    # VIP/Nedotriga are statuses for ordinary participants. Once a user receives
    # a full administrative rank, remove stale participant immunity so it cannot
    # silently reappear after a later demotion. Helper intentionally remains an
    # ordinary Telegram participant and is excluded from this cleanup.
    if role.name != HELPER_ROLE:
        removed_statuses = await remove_special_statuses_for_user(
            session,
            chat_id=chat_id,
            user_id=target_id,
        )
        if removed_statuses:
            await write_audit(
                session,
                "group.special_statuses_removed_on_admin_assignment",
                chat_id=chat_id,
                actor_user_id=actor_id,
                target_type="user",
                target_id=str(target_id),
                payload={
                    "role_name": role.name,
                    "statuses": removed_statuses,
                },
            )
    return None


async def sync_role_state_with_helper_lifecycle(
    callback,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
    enabled: bool,
) -> str | None:
    """When a mentor-capable rank is disabled, detach all of its Helpers.

    Re-enabling the rank never recreates old Helper relationships; they must be
    assigned again explicitly by an eligible administrator.
    """
    role = (
        await session.execute(
            select(AdminRole).where(
                AdminRole.id == role_id,
                AdminRole.chat_id == chat_id,
            )
        )
    ).scalar_one_or_none()
    mentor_ids: list[int] = []
    if role is not None and not enabled and role.name in MENTOR_ROLE_NAMES:
        mentor_ids = list((
            await session.execute(
                select(AdminAssignment.user_id).where(
                    AdminAssignment.chat_id == chat_id,
                    AdminAssignment.role_id == role_id,
                )
            )
        ).scalars().all())

    error = await _original_sync_role_state(
        callback,
        session,
        chat_id=chat_id,
        role_id=role_id,
        enabled=enabled,
    )
    if error is not None:
        return error

    for mentor_id in mentor_ids:
        await detach_helpers_from_mentor(
            session,
            chat_id=chat_id,
            mentor_id=int(mentor_id),
            actor_id=callback.from_user.id,
            reason="mentor_role_disabled",
        )
    return None


# Rank assignment functions are resolved through router module globals at
# callback execution time, so replacing them here covers all current rank paths.
_member_sync_module._assign_role = assign_role_with_mentor_lifecycle
_audit_actions_module._assign_role = assign_role_with_mentor_lifecycle
_compact_actions_module._assign_role = assign_role_with_mentor_lifecycle
_target_actions_module._assign_role = assign_role_with_mentor_lifecycle

# group_control_ux imported the state-sync function into its own module namespace,
# therefore both references must be replaced.
_role_actions_module._sync_managed_telegram_admins_for_role_state = sync_role_state_with_helper_lifecycle
_role_ux_module._sync_managed_telegram_admins_for_role_state = sync_role_state_with_helper_lifecycle
