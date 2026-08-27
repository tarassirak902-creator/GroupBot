from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AdminAssignment, AdminRole, Group, GroupStatus
from groupbot.routers.admin_hierarchy import RANK_META
from groupbot.routers.group_control import _owner_access
from groupbot.services.subscriptions import active_subscription_for_group

DEPUTY = "Зам. владельца"
CHIEF = "Глав. админ"
CHAT_ADMIN = "Администратор чата"
VOICE_ADMIN = "Администратор войса"
HELPER = "Помощник"
STANDARD_MANAGED = {CHIEF, CHAT_ADMIN, VOICE_ADMIN, HELPER}
CHIEF_ASSIGNABLE = {CHAT_ADMIN, VOICE_ADMIN, HELPER}


async def _rank_management_available(session: AsyncSession, chat_id: int) -> bool:
    status = (
        await session.execute(select(Group.status).where(Group.chat_id == chat_id))
    ).scalar_one_or_none()
    if status != GroupStatus.active.value:
        return False
    return await active_subscription_for_group(session, chat_id) is not None


async def _actor_role(session: AsyncSession, chat_id: int, actor_id: int) -> AdminRole | None:
    return (
        await session.execute(
            select(AdminRole)
            .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == actor_id,
                AdminRole.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def assignment_permission_error(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
    target_id: int,
    new_role: AdminRole,
    existing: AdminAssignment | None,
    old_role: AdminRole | None,
) -> str | None:
    if not await _rank_management_available(session, chat_id):
        return "Группа отключена или у неё нет активного тарифа. Управление администрацией временно недоступно."
    if await _owner_access(session, chat_id, actor_id):
        return None
    if actor_id == target_id:
        return "Нельзя изменять собственный административный ранг."

    actor_role = await _actor_role(session, chat_id, actor_id)
    if actor_role is None:
        return "У вас нет административного ранга, который позволяет управлять администрацией."

    # Custom ranks have no approved hierarchy yet, so only the owner may manage them.
    if new_role.name not in RANK_META or (old_role is not None and old_role.name not in RANK_META):
        return "Пользовательскими рангами пока может управлять только владелец группы."

    if actor_role.name == DEPUTY:
        if new_role.name not in STANDARD_MANAGED:
            return "Зам. владельца может назначать только ранги ниже своего."
        if old_role is not None and old_role.name not in STANDARD_MANAGED:
            return "Нельзя изменять ранг пользователя своего уровня или выше."
        return None

    if actor_role.name == CHIEF:
        if existing is not None and existing.role_id is not None:
            return "Глав. админ может назначать новые ранги, но не повышать и не понижать уже назначенных администраторов."
        if new_role.name in CHIEF_ASSIGNABLE:
            return None
        return "Глав. админ может назначать Администратора чата, Администратора войса или Помощника."

    if actor_role.name in {CHAT_ADMIN, VOICE_ADMIN}:
        if existing is not None and existing.role_id is not None:
            return "Администратор может назначать только нового Помощника, но не менять существующий ранг."
        if new_role.name == HELPER:
            return None
        return "Администратор чата/войса может назначать только Помощника."

    return "Ваш ранг не позволяет назначать, повышать или понижать администрацию."


async def removal_permission_error(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
    assignment: AdminAssignment,
    role: AdminRole,
) -> str | None:
    if not await _rank_management_available(session, chat_id):
        return "Группа отключена или у неё нет активного тарифа. Управление администрацией временно недоступно."
    if await _owner_access(session, chat_id, actor_id):
        return None
    if actor_id == assignment.user_id:
        return "Нельзя снять собственный административный ранг."

    actor_role = await _actor_role(session, chat_id, actor_id)
    if actor_role is None:
        return "У вас нет административного ранга, который позволяет снимать администрацию."

    if role.name not in RANK_META:
        return "Пользовательские ранги пока может снимать только владелец группы."

    if actor_role.name == DEPUTY:
        if role.name in STANDARD_MANAGED:
            return None
        return "Зам. владельца не может снимать пользователя своего уровня или выше."

    if actor_role.name == CHIEF:
        if role.name == HELPER:
            return None
        if role.name in {CHAT_ADMIN, VOICE_ADMIN}:
            if assignment.assigned_by_user_id == actor_id:
                return None
            return "Этого администратора назначил другой человек. Снять его может назначивший Глав. админ, Зам. владельца или Владелец."
        return "Глав. админ может снимать своих Администраторов чата/войса и любых Помощников."

    if actor_role.name in {CHAT_ADMIN, VOICE_ADMIN}:
        if role.name == HELPER and assignment.assigned_by_user_id == actor_id:
            return None
        if role.name == HELPER:
            return "Этого Помощника назначил другой администратор. Снять его может назначивший администратор, любой Глав. админ, Зам. владельца или Владелец."
        return "Администратор чата/войса может снимать только назначенных им Помощников."

    return "Ваш ранг не позволяет снимать администрацию."


async def can_open_rank_management(session: AsyncSession, *, chat_id: int, actor_id: int) -> bool:
    if not await _rank_management_available(session, chat_id):
        return False
    if await _owner_access(session, chat_id, actor_id):
        return True
    role = await _actor_role(session, chat_id, actor_id)
    return role is not None and role.name in {DEPUTY, CHIEF, CHAT_ADMIN, VOICE_ADMIN}
