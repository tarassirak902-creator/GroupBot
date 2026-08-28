from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, Group, GroupMember, MemberStatus, Transaction, User
from groupbot.moderation_models import ModerationAction, ObservedMessage
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_identity
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_group


PERIODS: dict[str, tuple[str, timedelta | None]] = {
    "day": ("за сутки", timedelta(days=1)),
    "week": ("за неделю", timedelta(days=7)),
    "month": ("за месяц", timedelta(days=30)),
    "all": ("за всё время", None),
}
TOP_LIMITS = {10, 20, 30}
GROUP_STATS_ROLES = {"Зам. владельца", "Глав. админ", "Администратор чата"}


def _period_since(period: str) -> datetime | None:
    _, delta = PERIODS.get(period, PERIODS["all"])
    return datetime.now(timezone.utc) - delta if delta is not None else None


async def _can_view_group_stats(session: AsyncSession, *, chat_id: int, user_id: int) -> bool:
    if await is_group_owner(session, chat_id, user_id):
        return True
    role_name = (
        await session.execute(
            select(AdminRole.name)
            .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == user_id,
                AdminRole.is_active.is_(True),
                AdminRole.name.in_(GROUP_STATS_ROLES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return role_name is not None


async def _count_messages(session: AsyncSession, chat_id: int, since: datetime | None = None) -> int:
    query = select(func.count()).select_from(ObservedMessage).where(ObservedMessage.chat_id == chat_id)
    if since is not None:
        query = query.where(ObservedMessage.sent_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _count_deleted_messages(session: AsyncSession, chat_id: int, since: datetime | None = None) -> int:
    query = select(func.count()).select_from(ObservedMessage).where(
        ObservedMessage.chat_id == chat_id,
        ObservedMessage.deleted_at.is_not(None),
    )
    if since is not None:
        query = query.where(ObservedMessage.deleted_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _known_members(session: AsyncSession, chat_id: int) -> int:
    return int((await session.execute(
        select(func.count()).select_from(GroupMember).where(GroupMember.chat_id == chat_id)
    )).scalar_one())


async def _active_authors(session: AsyncSession, chat_id: int, since: datetime | None) -> int:
    query = select(func.count(func.distinct(ObservedMessage.user_id))).where(ObservedMessage.chat_id == chat_id)
    if since is not None:
        query = query.where(ObservedMessage.sent_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _moderation_count(session: AsyncSession, chat_id: int, action: str, since: datetime | None) -> int:
    query = select(func.count()).select_from(ModerationAction).where(
        ModerationAction.chat_id == chat_id,
        ModerationAction.action == action,
    )
    if since is not None:
        query = query.where(ModerationAction.created_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _complaints_count(session: AsyncSession, chat_id: int, since: datetime | None) -> int:
    # Complaint mechanics are not yet stored in a dedicated table. Keep this
    # metric truthful until that module is added instead of deriving a fake value.
    return 0


async def _game_events_count(session: AsyncSession, chat_id: int, since: datetime | None) -> int:
    query = select(func.count()).select_from(Transaction).where(
        Transaction.chat_id == chat_id,
        Transaction.kind.like("game_%"),
    )
    if since is not None:
        query = query.where(Transaction.created_at >= since)
    return int((await session.execute(query)).scalar_one())


async def _top_users(session: AsyncSession, chat_id: int, since: datetime | None, limit: int):
    query = (
        select(
            User.telegram_user_id,
            User.first_name,
            User.last_name,
            User.username,
            func.count(ObservedMessage.message_id).label("message_count"),
        )
        .join(ObservedMessage, ObservedMessage.user_id == User.telegram_user_id)
        .where(ObservedMessage.chat_id == chat_id)
    )
    if since is not None:
        query = query.where(ObservedMessage.sent_at >= since)
    return (await session.execute(
        query
        .group_by(User.telegram_user_id, User.first_name, User.last_name, User.username)
        .order_by(func.count(ObservedMessage.message_id).desc(), User.telegram_user_id.asc())
        .limit(limit)
    )).all()


async def _build_stats(session: AsyncSession, chat_id: int, *, period: str, top_limit: int) -> str:
    if period not in PERIODS:
        period = "all"
    if top_limit not in TOP_LIMITS:
        top_limit = 10
    since = _period_since(period)
    period_label = PERIODS[period][0]

    group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
    title = group.title if group and group.title else str(chat_id)

    known = await _known_members(session, chat_id)
    authors = await _active_authors(session, chat_id, since)
    messages = await _count_messages(session, chat_id, since)
    deleted = await _count_deleted_messages(session, chat_id, since)
    warnings = await _moderation_count(session, chat_id, "warning", since)
    mutes = await _moderation_count(session, chat_id, "mute", since)
    bans = await _moderation_count(session, chat_id, "ban", since)
    complaints = await _complaints_count(session, chat_id, since)
    game_events = await _game_events_count(session, chat_id, since)
    top = await _top_users(session, chat_id, since, top_limit)

    lines = [
        "📊 <b>СТАТИСТИКА ГРУППЫ</b>",
        f"🏠 {escape(title)}",
        f"🕘 Период: {period_label}",
        "",
        f"👥 Сейчас известно в группе: <b>{known}</b>",
        f"✍️ Активных авторов: <b>{authors}</b>",
        f"💬 Сообщений: <b>{messages}</b>",
        f"🗑 Удалено сообщений: <b>{deleted}</b>",
        "",
        "⚖️ <b>МОДЕРАЦИЯ</b>",
        f"⚠️ Предупреждений: <b>{warnings}</b>",
        f"🔇 Мутов: <b>{mutes}</b>",
        f"🚫 Банов: <b>{bans}</b>",
        f"🚨 Жалоб: <b>{complaints}</b>",
        "",
        f"🎮 Игровых событий: <b>{game_events}</b>",
        "",
        "🔥 <b>Самые активные:</b>",
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

    lines.extend(["", "Статистика относится только к этой группе."])
    return "\n".join(lines)


def _stats_keyboard(chat_id: int, *, period: str, top_limit: int, private: bool) -> InlineKeyboardMarkup:
    def period_text(key: str, label: str) -> str:
        return ("• " if period == key else "") + label

    def top_text(value: int) -> str:
        return ("• " if top_limit == value else "") + f"Топ {value}"

    rows = [
        [
            InlineKeyboardButton(text=period_text("day", "🌅 Сутки"), callback_data=f"analytics:view:{chat_id}:day:{top_limit}"),
            InlineKeyboardButton(text=period_text("week", "📅 Неделя"), callback_data=f"analytics:view:{chat_id}:week:{top_limit}"),
        ],
        [
            InlineKeyboardButton(text=period_text("month", "🗓 Месяц"), callback_data=f"analytics:view:{chat_id}:month:{top_limit}"),
            InlineKeyboardButton(text=period_text("all", "∞ Всё время"), callback_data=f"analytics:view:{chat_id}:all:{top_limit}"),
        ],
        [InlineKeyboardButton(text=top_text(10), callback_data=f"analytics:view:{chat_id}:{period}:10")],
        [
            InlineKeyboardButton(text=top_text(20), callback_data=f"analytics:view:{chat_id}:{period}:20"),
            InlineKeyboardButton(text=top_text(30), callback_data=f"analytics:view:{chat_id}:{period}:30"),
        ],
    ]
    if private:
        rows.append([InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:open:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _callback_access(
    session: AsyncSession,
    callback: CallbackQuery,
    chat_id: int,
) -> tuple[bool, bool]:
    """Return (allowed, private_screen)."""
    private_screen = bool(callback.message and callback.message.chat.type == "private")
    if await active_subscription_for_group(session, chat_id) is None:
        return False, private_screen
    allowed = await _can_view_group_stats(
        session,
        chat_id=chat_id,
        user_id=callback.from_user.id,
    )
    return allowed, private_screen


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
            text = await _build_stats(session, chat_id, period="all", top_limit=10)
        if callback.message is not None:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_stats_keyboard(chat_id, period="all", top_limit=10, private=True),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("analytics:view:"))
    async def analytics_view(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 5:
            return
        try:
            chat_id = int(parts[2])
            period = parts[3]
            top_limit = int(parts[4])
        except ValueError:
            return
        if period not in PERIODS or top_limit not in TOP_LIMITS:
            await callback.answer("Некорректный период или размер топа.", show_alert=True)
            return
        async with session_factory() as session:
            allowed, private_screen = await _callback_access(session, callback, chat_id)
            if not allowed:
                await callback.answer(
                    "Полную статистику группы могут смотреть только Владелец, Зам. владельца, Глав. админ и Администратор чата.",
                    show_alert=True,
                )
                return
            text = await _build_stats(session, chat_id, period=period, top_limit=top_limit)
        if callback.message is not None:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_stats_keyboard(chat_id, period=period, top_limit=top_limit, private=private_screen),
            )
        await callback.answer()

    @router.message(Command("groupstats"), F.chat.type.in_({"group", "supergroup"}))
    async def group_stats(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if await active_subscription_for_group(session, message.chat.id) is None:
                return
            if not await _can_view_group_stats(
                session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
            ):
                await message.reply(
                    "Полную статистику группы могут смотреть только Владелец, Зам. владельца, Глав. админ и Администратор чата."
                )
                return
            text = await _build_stats(session, message.chat.id, period="all", top_limit=10)
        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_stats_keyboard(message.chat.id, period="all", top_limit=10, private=False),
        )

    return router
