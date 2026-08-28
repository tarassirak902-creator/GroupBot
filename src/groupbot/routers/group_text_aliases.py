from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message, User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, AuditLog, GroupMember, GroupOwner, GroupSettings, MemberStatus, User
from groupbot.moderation_models import ModerationAction
from groupbot.routers.group_profile_stats import _access_allowed, _fmt_dt, _message_count, _rank_name, _special_statuses, _warning_count
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.helper_role_policy import HELPER_ROLE
from groupbot.services.permissions import is_group_owner


async def _can_view_other_profile(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
) -> bool:
    if await is_group_owner(session, chat_id, actor_id):
        return True
    assignment_id = (
        await session.execute(
            select(AdminAssignment.id)
            .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == actor_id,
                AdminRole.is_active.is_(True),
                AdminRole.name != HELPER_ROLE,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return assignment_id is not None


async def _helper_profile_extra(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> tuple[str, int]:
    violation_count = int((
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.chat_id == chat_id,
                AuditLog.actor_user_id == user_id,
                AuditLog.event_type == "group.helper_violation_reported",
            )
        )
    ).scalar_one())

    assignment = (
        await session.execute(
            select(AdminAssignment)
            .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == user_id,
                AdminRole.name == HELPER_ROLE,
            )
        )
    ).scalar_one_or_none()
    mentor_id = assignment.assigned_by_user_id if assignment is not None else None

    if mentor_id is None:
        audit_rows = (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.chat_id == chat_id,
                    AuditLog.event_type == "group.admin_rank_assigned",
                    AuditLog.target_type == "user",
                    AuditLog.target_id == str(user_id),
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(20)
            )
        ).scalars().all()
        for audit_row in audit_rows:
            if (
                (audit_row.payload or {}).get("role_name") == HELPER_ROLE
                and audit_row.actor_user_id is not None
            ):
                mentor_id = audit_row.actor_user_id
                break

    if mentor_id is None:
        return "⚠️ не определён", violation_count

    mentor = (
        await session.execute(
            select(User).where(User.telegram_user_id == mentor_id)
        )
    ).scalar_one_or_none()
    mentor_text = (
        clickable_user_display(mentor)
        if mentor is not None
        else clickable_identity(
            telegram_user_id=mentor_id,
            first_name="Наставник",
            username=None,
        )
    )
    return mentor_text, violation_count


async def _admin_profile_extra(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> tuple[int, int, int, int]:
    action_rows = (
        await session.execute(
            select(ModerationAction.action, func.count())
            .where(
                ModerationAction.chat_id == chat_id,
                ModerationAction.actor_user_id == user_id,
                ModerationAction.action.in_(["warning", "mute", "ban"]),
            )
            .group_by(ModerationAction.action)
        )
    ).all()
    action_counts = {str(action): int(count) for action, count in action_rows}

    helper_count = int((
        await session.execute(
            select(func.count())
            .select_from(AdminAssignment)
            .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.assigned_by_user_id == user_id,
                AdminRole.name == HELPER_ROLE,
                AdminRole.is_active.is_(True),
            )
        )
    ).scalar_one())

    return (
        action_counts.get("warning", 0),
        action_counts.get("mute", 0),
        action_counts.get("ban", 0),
        helper_count,
    )


async def _admin_leaderboard_text(session: AsyncSession, *, chat_id: int) -> str:
    owner_ids = set((
        await session.execute(
            select(GroupOwner.user_id).where(
                GroupOwner.chat_id == chat_id,
                GroupOwner.is_current.is_(True),
            )
        )
    ).scalars().all())

    admin_rows = (
        await session.execute(
            select(AdminAssignment.user_id, AdminRole.name)
            .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminRole.is_active.is_(True),
                AdminRole.name != HELPER_ROLE,
            )
        )
    ).all()
    role_by_user = {int(user_id): str(role_name) for user_id, role_name in admin_rows}
    admin_ids = set(role_by_user) | {int(user_id) for user_id in owner_ids}

    if not admin_ids:
        return "🏆 <b>Топ администрации</b>\n\nВ группе пока нет действующей администрации Mimorus."

    action_rows = (
        await session.execute(
            select(ModerationAction.actor_user_id, ModerationAction.action, func.count())
            .where(
                ModerationAction.chat_id == chat_id,
                ModerationAction.actor_user_id.in_(admin_ids),
                ModerationAction.action.in_(["warning", "mute", "ban"]),
            )
            .group_by(ModerationAction.actor_user_id, ModerationAction.action)
        )
    ).all()
    actions_by_user: dict[int, dict[str, int]] = {user_id: {} for user_id in admin_ids}
    for user_id, action, count in action_rows:
        actions_by_user[int(user_id)][str(action)] = int(count)

    helper_rows = (
        await session.execute(
            select(AdminAssignment.user_id, AdminAssignment.assigned_by_user_id)
            .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.assigned_by_user_id.in_(admin_ids),
                AdminRole.name == HELPER_ROLE,
                AdminRole.is_active.is_(True),
            )
        )
    ).all()
    helpers_by_mentor: dict[int, list[int]] = {user_id: [] for user_id in admin_ids}
    for helper_id, mentor_id in helper_rows:
        if mentor_id is None:
            continue
        helpers_by_mentor.setdefault(int(mentor_id), []).append(int(helper_id))

    helper_reports_by_mentor: dict[int, int] = {user_id: 0 for user_id in admin_ids}
    report_rows = (
        await session.execute(
            select(AuditLog.payload)
            .where(
                AuditLog.chat_id == chat_id,
                AuditLog.event_type == "group.helper_violation_reported",
            )
        )
    ).scalars().all()
    for payload in report_rows:
        raw_mentor_id = (payload or {}).get("assigned_admin_id")
        try:
            mentor_id = int(raw_mentor_id)
        except (TypeError, ValueError):
            continue
        if mentor_id in admin_ids:
            helper_reports_by_mentor[mentor_id] = helper_reports_by_mentor.get(mentor_id, 0) + 1

    users = (
        await session.execute(
            select(User).where(User.telegram_user_id.in_(admin_ids))
        )
    ).scalars().all()
    users_by_id = {int(user.telegram_user_id): user for user in users}

    ranking: list[tuple[int, int, int, int, int, int]] = []
    for user_id in admin_ids:
        counts = actions_by_user.get(user_id, {})
        warnings = counts.get("warning", 0)
        mutes = counts.get("mute", 0)
        bans = counts.get("ban", 0)
        total = warnings + mutes + bans
        helper_reports = helper_reports_by_mentor.get(user_id, 0)
        ranking.append((user_id, total, helper_reports, warnings, mutes, bans))

    ranking.sort(key=lambda row: (-row[1], -row[2], row[0]))

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        "🏆 <b>Топ администрации</b>",
        "",
        "Сортировка: по количеству фактических предов, мутов и банов.",
        "",
    ]
    for index, (user_id, total, helper_reports, warnings, mutes, bans) in enumerate(ranking[:10], start=1):
        user = users_by_id.get(user_id)
        identity = (
            clickable_user_display(user)
            if user is not None
            else clickable_identity(
                telegram_user_id=user_id,
                first_name="Администратор",
                username=None,
            )
        )
        role_name = "Владелец группы" if user_id in owner_ids else role_by_user.get(user_id, "Администратор")
        prefix = medals[index - 1] if index <= len(medals) else f"{index}."
        helper_count = len(helpers_by_mentor.get(user_id, []))
        lines.extend([
            f"{prefix} {identity} — <b>{role_name}</b>",
            f"   📌 Действий: <b>{total}</b> · ⚠️ {warnings} · 🔇 {mutes} · ⛔ {bans}",
            f"   🔹 Помощников: <b>{helper_count}</b> · 🚨 нашли нарушений: <b>{helper_reports}</b>",
            "",
        ])

    return "\n".join(lines).rstrip()


async def _profile_text(
    session: AsyncSession,
    *,
    chat_id: int,
    target: TelegramUser,
) -> str:
    user = (
        await session.execute(
            select(User).where(User.telegram_user_id == target.id)
        )
    ).scalar_one_or_none()
    member = (
        await session.execute(
            select(GroupMember).where(
                GroupMember.chat_id == chat_id,
                GroupMember.user_id == target.id,
            )
        )
    ).scalar_one_or_none()
    settings = (
        await session.execute(
            select(GroupSettings).where(GroupSettings.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    messages = await _message_count(session, chat_id, target.id)
    warnings = await _warning_count(session, chat_id, target.id)
    rank = await _rank_name(session, chat_id, target.id)
    owner = await is_group_owner(session, chat_id, target.id)

    helper_mentor_text = "⚠️ не определён"
    helper_violation_count = 0
    if rank == HELPER_ROLE:
        helper_mentor_text, helper_violation_count = await _helper_profile_extra(
            session,
            chat_id=chat_id,
            user_id=target.id,
        )

    admin_warnings = 0
    admin_mutes = 0
    admin_bans = 0
    admin_helpers = 0
    if owner or (rank is not None and rank != HELPER_ROLE):
        admin_warnings, admin_mutes, admin_bans, admin_helpers = await _admin_profile_extra(
            session,
            chat_id=chat_id,
            user_id=target.id,
        )

    identity = clickable_identity(
        telegram_user_id=target.id,
        first_name=(user.first_name if user else target.first_name),
        last_name=(user.last_name if user else target.last_name),
        username=(user.username if user else target.username),
    )
    statuses = _special_statuses(settings.moderation_config if settings else {}, target.id)
    status_text = "участник"
    if member is not None and member.status != MemberStatus.member.value:
        status_text = member.status
    admin_line = rank or ("Владелец группы" if owner else "—")
    special_line = ", ".join(statuses) if statuses else "—"
    helper_line = (
        f"\n🧭 Наставник: {helper_mentor_text}"
        f"\n🚨 Помог найти нарушений: <b>{helper_violation_count}</b>"
        if rank == HELPER_ROLE
        else ""
    )
    admin_stats_line = (
        "\n\n📊 <b>Статистика администратора</b>"
        f"\n⚠️ Выдано предупреждений: <b>{admin_warnings}</b>"
        f"\n🔇 Выдано мутов: <b>{admin_mutes}</b>"
        f"\n⛔ Выдано банов: <b>{admin_bans}</b>"
        f"\n🔹 Помощников закреплено: <b>{admin_helpers}</b>"
        if owner or (rank is not None and rank != HELPER_ROLE)
        else ""
    )
    return (
        "👤 <b>Профиль участника</b>\n\n"
        f"Пользователь: {identity}\n"
        f"Статус в группе: <b>{status_text}</b>\n"
        f"Ранг Mimorus: <b>{admin_line}</b>\n"
        f"Особый статус: <b>{special_line}</b>"
        f"{helper_line}\n\n"
        f"Первое появление: <b>{_fmt_dt(member.first_seen_at if member else None)}</b>\n"
        f"Последняя активность: <b>{_fmt_dt(member.last_activity_at if member else None)}</b>\n"
        f"Сообщений учтено: <b>{messages}</b>\n"
        f"Активных предупреждений: <b>{warnings}</b>"
        f"{admin_stats_line}"
    )


def create_group_text_aliases_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="group_text_aliases")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*кто\s+я\s*[?？]?\s*$"),
    )
    async def who_am_i(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if not await _access_allowed(session, message.chat.id):
                return
            text = await _profile_text(
                session,
                chat_id=message.chat.id,
                target=message.from_user,
            )
        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.regexp(r"(?i)^\s*кто\s+(?:он|она|ты)\s*[?？]?\s*$"),
    )
    async def who_is_reply_target(message: Message) -> None:
        if message.from_user is None or message.reply_to_message is None:
            return
        target = message.reply_to_message.from_user
        if target is None:
            await message.reply("Не удалось определить пользователя из сообщения.")
            return

        async with session_factory() as session:
            if not await _access_allowed(session, message.chat.id):
                return
            if not await _can_view_other_profile(
                session,
                chat_id=message.chat.id,
                actor_id=message.from_user.id,
            ):
                await message.reply(
                    "Эта команда доступна владельцу и действующим администраторам группы."
                )
                return
            text = await _profile_text(
                session,
                chat_id=message.chat.id,
                target=target,
            )

        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*топ\s+админ(?:ов|истрации)?\s*[?？]?\s*$"),
    )
    async def admin_leaderboard(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if not await _access_allowed(session, message.chat.id):
                return
            if not await _can_view_other_profile(
                session,
                chat_id=message.chat.id,
                actor_id=message.from_user.id,
            ):
                await message.reply(
                    "Эта команда доступна владельцу и действующим администраторам группы."
                )
                return
            text = await _admin_leaderboard_text(session, chat_id=message.chat.id)

        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    return router
