from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, Subscription, SubscriptionStatus, Tariff, User
from groupbot.routers.user_display import clickable_user_display


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _user_link(user: User) -> str:
    return clickable_user_display(user)


def _user_card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Группы пользователя", callback_data=f"creator:user_groups:{user_id}")],
            [InlineKeyboardButton(text="💳 Управление подпиской", callback_data=f"creator:user_sub:{user_id}")],
            [InlineKeyboardButton(text="📋 История подписок", callback_data=f"creator:user_history:{user_id}")],
            [InlineKeyboardButton(text="🔎 Диагностика пользователя", callback_data=f"creator:user_diag:{user_id}")],
            [InlineKeyboardButton(text="◀️ Пользователи", callback_data="creator:users")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _user_back_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Карточка пользователя", callback_data=f"creator:usercard:{user_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _group_status_icon(status: str) -> str:
    return {"active": "✅", "pending": "⏳", "disabled": "⚠️", "left": "❌"}.get(status, "•")


def create_creator_user_profile_links_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    router = Router(name="creator_user_profile_links")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    async def get_user(session: AsyncSession, user_id: int) -> User | None:
        return (
            await session.execute(select(User).where(User.telegram_user_id == user_id))
        ).scalar_one_or_none()

    async def get_active_subscription(session: AsyncSession, user_id: int):
        return (
            await session.execute(
                select(Subscription, Tariff)
                .join(Tariff, Tariff.id == Subscription.tariff_id)
                .where(
                    Subscription.owner_user_id == user_id,
                    Subscription.status == SubscriptionStatus.active.value,
                    Subscription.ends_at > datetime.now(timezone.utc),
                )
                .order_by(Subscription.ends_at.desc())
                .limit(1)
            )
        ).first()

    @router.callback_query(F.data.startswith("creator:usercard:"))
    async def user_card(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        async with session_factory() as session:
            user = await get_user(session, user_id)
            owned_count = (
                await session.execute(
                    select(func.count()).select_from(GroupOwner).where(
                        GroupOwner.user_id == user_id,
                        GroupOwner.is_current.is_(True),
                    )
                )
            ).scalar_one()
            active = await get_active_subscription(session, user_id)
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        tariff_text = (
            f"{escape(active.Tariff.name)} до {_fmt_dt(active.Subscription.ends_at)}"
            if active
            else "нет активного"
        )
        await callback.message.edit_text(
            "👤 <b>Карточка пользователя</b>\n\n"
            f"Пользователь: {_user_link(user)}\n"
            f"Telegram Premium: {'✅' if user.is_premium else '❌'}\n"
            f"Владеет группами: <b>{owned_count}</b>\n"
            f"Тариф: <b>{tariff_text}</b>\n"
            f"Первый контакт: <b>{_fmt_dt(user.first_seen_at)}</b>\n"
            f"Обновлён: <b>{_fmt_dt(user.updated_at)}</b>",
            parse_mode="HTML",
            reply_markup=_user_card_keyboard(user_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_groups:"))
    async def user_groups(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        async with session_factory() as session:
            user = await get_user(session, user_id)
            rows = (
                await session.execute(
                    select(Group.chat_id, Group.title, Group.status)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(GroupOwner.user_id == user_id, GroupOwner.is_current.is_(True))
                    .order_by(Group.connected_at.desc().nullslast(), Group.chat_id)
                )
            ).all()
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        buttons: list[list[InlineKeyboardButton]] = []
        for chat_id, title, status in rows:
            buttons.append([
                InlineKeyboardButton(
                    text=f"{_group_status_icon(status)} {title or chat_id}"[:64],
                    callback_data=f"creator:group:{chat_id}",
                )
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Карточка пользователя", callback_data=f"creator:usercard:{user_id}")])
        await callback.message.edit_text(
            "👥 <b>Группы пользователя</b>\n\n"
            f"Пользователь: {_user_link(user)}\n"
            f"Групп: <b>{len(rows)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_history:"))
    async def user_history(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        async with session_factory() as session:
            user = await get_user(session, user_id)
            rows = (
                await session.execute(
                    select(Subscription, Tariff)
                    .join(Tariff, Tariff.id == Subscription.tariff_id)
                    .where(Subscription.owner_user_id == user_id)
                    .order_by(Subscription.created_at.desc(), Subscription.id.desc())
                    .limit(10)
                )
            ).all()
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        lines = ["📋 <b>История подписок</b>", "", f"Пользователь: {_user_link(user)}", ""]
        if not rows:
            lines.append("История подписок пуста.")
        else:
            status_icons = {"active": "✅", "expired": "⌛", "cancelled": "⛔"}
            for subscription, tariff in rows:
                lines.extend([
                    f"{status_icons.get(subscription.status, '•')} <b>{escape(tariff.code)}</b> — {escape(subscription.status)}",
                    f"   {_fmt_dt(subscription.started_at)} → {_fmt_dt(subscription.ends_at)}",
                ])
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=_user_back_keyboard(user_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_diag:"))
    async def user_diag(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        async with session_factory() as session:
            user = await get_user(session, user_id)
            owned_groups = (
                await session.execute(
                    select(func.count()).select_from(GroupOwner).where(
                        GroupOwner.user_id == user_id,
                        GroupOwner.is_current.is_(True),
                    )
                )
            ).scalar_one()
            total_subscriptions = (
                await session.execute(
                    select(func.count()).select_from(Subscription).where(Subscription.owner_user_id == user_id)
                )
            ).scalar_one()
            active = await get_active_subscription(session, user_id)
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        active_text = (
            f"{escape(active.Tariff.code)} до {_fmt_dt(active.Subscription.ends_at)}"
            if active
            else "нет"
        )
        await callback.message.edit_text(
            "🔎 <b>Диагностика пользователя</b>\n\n"
            f"Пользователь: {_user_link(user)}\n"
            f"Удалённый аккаунт: {'⚠️ да' if user.deleted_account else '✅ нет'}\n"
            f"Telegram Premium: {'✅ да' if user.is_premium else '❌ нет'}\n"
            f"Текущих групп владельца: <b>{owned_groups}</b>\n"
            f"Записей подписок: <b>{total_subscriptions}</b>\n"
            f"Активная подписка: <b>{active_text}</b>\n"
            f"Первый контакт: <b>{_fmt_dt(user.first_seen_at)}</b>\n"
            f"Последнее обновление профиля: <b>{_fmt_dt(user.updated_at)}</b>",
            parse_mode="HTML",
            reply_markup=_user_back_keyboard(user_id),
        )
        await callback.answer()

    return router
