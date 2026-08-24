from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, GroupMember, GroupSettings, MemberStatus, User
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.user_display import clickable_identity
from groupbot.services.subscriptions import active_subscription_for_group


async def _access_allowed(session: AsyncSession, chat_id: int) -> bool:
    return await active_subscription_for_group(session, chat_id) is not None


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


async def _warning_count(session: AsyncSession, chat_id: int, user_id: int) -> int:
    return int((await session.execute(
        select(func.count()).select_from(ModerationAction).where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_user_id == user_id,
            ModerationAction.action == "warning",
            ModerationAction.is_active.is_(True),
        )
    )).scalar_one())


async def _message_count(session: AsyncSession, chat_id: int, user_id: int, since: datetime | None = None) -> int:
    query = select(func.count()).select_from(ObservedMessage).where(
        ObservedMessage.chat_id == chat_id,
        ObservedMessage.user_id == user_id,
    )
    if since is not None:
        query = query.where(ObservedMessage.sent_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _rank_name(session: AsyncSession, chat_id: int, user_id: int) -> str | None:
    return (await session.execute(
        select(AdminRole.name)
        .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
        .where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == user_id)
        .limit(1)
    )).scalar_one_or_none()


def _special_statuses(config: dict | None, user_id: int) -> list[str]:
    special = dict((config or {}).get("special_statuses") or {})
    result: list[str] = []
    if user_id in {int(x) for x in (special.get("vip") or [])}:
        result.append("💎 VIP")
    if user_id in {int(x) for x in (special.get("nedotroga") or [])}:
        result.append("🛡 Недотрога")
    return result


def create_group_profile_stats_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="group_profile_stats")

    @router.message(Command("profile"), F.chat.type.in_({"group", "supergroup"}))
    async def profile(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if not await _access_allowed(session, message.chat.id):
                return
            user = (await session.execute(select(User).where(User.telegram_user_id == message.from_user.id))).scalar_one_or_none()
            member = (await session.execute(select(GroupMember).where(
                GroupMember.chat_id == message.chat.id,
                GroupMember.user_id == message.from_user.id,
            ))).scalar_one_or_none()
            settings = (await session.execute(select(GroupSettings).where(GroupSettings.chat_id == message.chat.id))).scalar_one_or_none()
            messages = await _message_count(session, message.chat.id, message.from_user.id)
            warnings = await _warning_count(session, message.chat.id, message.from_user.id)
            rank = await _rank_name(session, message.chat.id, message.from_user.id)

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
        await message.answer(
            "👤 <b>Профиль участника</b>\n\n"
            f"Пользователь: {identity}\n"
            f"Статус в группе: <b>{status_text}</b>\n"
            f"Ранг Mimorus: <b>{admin_line}</b>\n"
            f"Особый статус: <b>{special_line}</b>\n\n"
            f"Первое появление: <b>{_fmt_dt(member.first_seen_at if member else None)}</b>\n"
            f"Последняя активность: <b>{_fmt_dt(member.last_activity_at if member else None)}</b>\n"
            f"Сообщений учтено: <b>{messages}</b>\n"
            f"Активных предупреждений: <b>{warnings}</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
    async def stats(message: Message) -> None:
        if message.from_user is None:
            return
        now = datetime.now(timezone.utc)
        start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        async with session_factory() as session:
            if not await _access_allowed(session, message.chat.id):
                return
            member = (await session.execute(select(GroupMember).where(
                GroupMember.chat_id == message.chat.id,
                GroupMember.user_id == message.from_user.id,
            ))).scalar_one_or_none()
            today = await _message_count(session, message.chat.id, message.from_user.id, start_today)
            week = await _message_count(session, message.chat.id, message.from_user.id, now - timedelta(days=7))
            month = await _message_count(session, message.chat.id, message.from_user.id, now - timedelta(days=30))
            total = await _message_count(session, message.chat.id, message.from_user.id)
            warnings = await _warning_count(session, message.chat.id, message.from_user.id)

        identity = clickable_identity(
            telegram_user_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        await message.answer(
            "📊 <b>Моя активность</b>\n\n"
            f"Пользователь: {identity}\n\n"
            f"Сегодня: <b>{today}</b> сообщений\n"
            f"За 7 дней: <b>{week}</b>\n"
            f"За 30 дней: <b>{month}</b>\n"
            f"За всё время наблюдения: <b>{total}</b>\n\n"
            f"Удалено сообщений: <b>{member.deleted_messages if member else 0}</b>\n"
            f"Активных предупреждений: <b>{warnings}</b>\n"
            f"Последняя активность: <b>{_fmt_dt(member.last_activity_at if member else None)}</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    return router
