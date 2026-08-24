from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupMember, MemberStatus, User
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_identity
from groupbot.services.permissions import has_permission
from groupbot.services.subscriptions import active_subscription_for_group


async def _count_messages(session: AsyncSession, chat_id: int, since: datetime | None = None) -> int:
    query = select(func.count()).select_from(ObservedMessage).where(ObservedMessage.chat_id == chat_id)
    if since is not None:
        query = query.where(ObservedMessage.sent_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _count_members(session: AsyncSession, chat_id: int, status: str | None = None) -> int:
    query = select(func.count()).select_from(GroupMember).where(GroupMember.chat_id == chat_id)
    if status is not None:
        query = query.where(GroupMember.status == status)
    return int((await session.execute(query)).scalar_one())


async def _count_joined(session: AsyncSession, chat_id: int, since: datetime) -> int:
    return int((await session.execute(
        select(func.count()).select_from(GroupMember).where(
            GroupMember.chat_id == chat_id,
            GroupMember.joined_at.is_not(None),
            GroupMember.joined_at >= since,
        )
    )).scalar_one())


async def _count_left(session: AsyncSession, chat_id: int, since: datetime) -> int:
    return int((await session.execute(
        select(func.count()).select_from(GroupMember).where(
            GroupMember.chat_id == chat_id,
            GroupMember.left_at.is_not(None),
            GroupMember.left_at >= since,
        )
    )).scalar_one())


async def _active_actions(session: AsyncSession, chat_id: int, action: str) -> int:
    return int((await session.execute(
        select(func.count()).select_from(ModerationAction).where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.action == action,
            ModerationAction.is_active.is_(True),
        )
    )).scalar_one())


async def _top_users(session: AsyncSession, chat_id: int, since: datetime, limit: int = 5):
    rows = (await session.execute(
        select(
            User.telegram_user_id,
            User.first_name,
            User.last_name,
            User.username,
            func.count(ObservedMessage.message_id).label("message_count"),
        )
        .join(ObservedMessage, ObservedMessage.user_id == User.telegram_user_id)
        .where(
            ObservedMessage.chat_id == chat_id,
            ObservedMessage.sent_at >= since,
        )
        .group_by(User.telegram_user_id, User.first_name, User.last_name, User.username)
        .order_by(func.count(ObservedMessage.message_id).desc(), User.telegram_user_id.asc())
        .limit(limit)
    )).all()
    return rows


async def _build_stats(session: AsyncSession, chat_id: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
    title = group.title if group and group.title else str(chat_id)

    active_members = await _count_members(session, chat_id, MemberStatus.member.value)
    known_members = await _count_members(session, chat_id)
    messages_today = await _count_messages(session, chat_id, start_today)
    messages_week = await _count_messages(session, chat_id, week_ago)
    messages_month = await _count_messages(session, chat_id, month_ago)
    messages_total = await _count_messages(session, chat_id)
    joined_week = await _count_joined(session, chat_id, week_ago)
    left_week = await _count_left(session, chat_id, week_ago)
    warnings = await _active_actions(session, chat_id, "warning")
    mutes = await _active_actions(session, chat_id, "mute")
    bans = await _active_actions(session, chat_id, "ban")
    top = await _top_users(session, chat_id, week_ago)

    lines = [
        "📊 <b>Статистика группы</b>",
        "",
        f"Группа: <b>{escape(title)}</b>",
        "",
        "👥 <b>Аудитория</b>",
        f"Активных участников: <b>{active_members}</b>",
        f"Всего известных Mimorus: <b>{known_members}</b>",
        f"Новых за 7 дней: <b>{joined_week}</b>",
        f"Ушло за 7 дней: <b>{left_week}</b>",
        "",
        "💬 <b>Активность</b>",
        f"Сегодня: <b>{messages_today}</b> сообщений",
        f"За 7 дней: <b>{messages_week}</b>",
        f"За 30 дней: <b>{messages_month}</b>",
        f"За всё время наблюдения: <b>{messages_total}</b>",
        "",
        "🛡 <b>Модерация</b>",
        f"Активных предупреждений: <b>{warnings}</b>",
        f"Активных мутов: <b>{mutes}</b>",
        f"Активных банов: <b>{bans}</b>",
        "",
        "🏆 <b>Топ активности за 7 дней</b>",
    ]
    if not top:
        lines.append("Пока недостаточно данных.")
    else:
        for index, row in enumerate(top, start=1):
            identity = clickable_identity(
                telegram_user_id=row.telegram_user_id,
                first_name=row.first_name,
                last_name=row.last_name,
                username=row.username,
            )
            lines.append(f"{index}. {identity} — <b>{row.message_count}</b>")

    return "\n".join(lines), title


def _owner_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"analytics:refresh:{chat_id}")],
        [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
    ])


def create_group_analytics_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="group_analytics")

    @router.callback_query(F.data.startswith("group:section:") & F.data.endswith(":statistics"))
    async def owner_stats(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return
            text, _ = await _build_stats(session, chat_id)
        if callback.message is not None:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_owner_keyboard(chat_id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("analytics:refresh:"))
    async def refresh(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            text, _ = await _build_stats(session, chat_id)
        if callback.message is not None:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_owner_keyboard(chat_id),
            )
        await callback.answer("Обновлено")

    @router.message(Command("groupstats"), F.chat.type.in_({"group", "supergroup"}))
    async def group_stats(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if await active_subscription_for_group(session, message.chat.id) is None:
                return
            if not await has_permission(session, message.chat.id, message.from_user.id, "stats"):
                await message.reply("Недостаточно прав Mimorus для просмотра полной статистики.")
                return
            text, _ = await _build_stats(session, message.chat.id)
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    return router
