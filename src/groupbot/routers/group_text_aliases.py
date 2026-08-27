from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, AuditLog, GroupMember, GroupSettings, MemberStatus, User
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.group_profile_stats import _access_allowed, _fmt_dt, _message_count, _rank_name, _special_statuses, _warning_count
from groupbot.routers.user_display import clickable_identity
from groupbot.services.helper_role_policy import HELPER_ROLE


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
            user = (
                await session.execute(
                    select(User).where(User.telegram_user_id == message.from_user.id)
                )
            ).scalar_one_or_none()
            member = (
                await session.execute(
                    select(GroupMember).where(
                        GroupMember.chat_id == message.chat.id,
                        GroupMember.user_id == message.from_user.id,
                    )
                )
            ).scalar_one_or_none()
            settings = (
                await session.execute(
                    select(GroupSettings).where(GroupSettings.chat_id == message.chat.id)
                )
            ).scalar_one_or_none()
            messages = await _message_count(session, message.chat.id, message.from_user.id)
            warnings = await _warning_count(session, message.chat.id, message.from_user.id)
            rank = await _rank_name(session, message.chat.id, message.from_user.id)
            helper_violation_count = 0
            if rank == HELPER_ROLE:
                helper_violation_count = int((
                    await session.execute(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(
                            AuditLog.chat_id == message.chat.id,
                            AuditLog.actor_user_id == message.from_user.id,
                            AuditLog.event_type == "group.helper_violation_reported",
                        )
                    )
                ).scalar_one())

        identity = clickable_identity(
            telegram_user_id=message.from_user.id,
            first_name=(user.first_name if user else message.from_user.first_name),
            last_name=(user.last_name if user else message.from_user.last_name),
            username=(user.username if user else message.from_user.username),
        )
        statuses = _special_statuses(settings.moderation_config if settings else {}, message.from_user.id)
        status_text = "участник"
        if member is not None and member.status != MemberStatus.member.value:
            status_text = member.status
        admin_line = rank or "—"
        special_line = ", ".join(statuses) if statuses else "—"
        helper_line = (
            f"\n🚨 Помог найти нарушений: <b>{helper_violation_count}</b>"
            if rank == HELPER_ROLE
            else ""
        )
        await message.answer(
            "👤 <b>Профиль участника</b>\n\n"
            f"Пользователь: {identity}\n"
            f"Статус в группе: <b>{status_text}</b>\n"
            f"Ранг Mimorus: <b>{admin_line}</b>\n"
            f"Особый статус: <b>{special_line}</b>"
            f"{helper_line}\n\n"
            f"Первое появление: <b>{_fmt_dt(member.first_seen_at if member else None)}</b>\n"
            f"Последняя активность: <b>{_fmt_dt(member.last_activity_at if member else None)}</b>\n"
            f"Сообщений учтено: <b>{messages}</b>\n"
            f"Активных предупреждений: <b>{warnings}</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    return router
