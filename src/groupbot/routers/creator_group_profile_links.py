from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, Subscription, SubscriptionStatus, Tariff, User
from groupbot.routers.user_display import clickable_user_display


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _group_status_icon(status: str) -> str:
    return {"active": "✅", "pending": "⏳", "disabled": "⚠️", "left": "❌"}.get(status, "•")


def _group_card_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Проверить права бота", callback_data=f"creator:group_diag:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Все группы", callback_data="creator:groups")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _group_title(title: str | None, username: str | None) -> str:
    safe_title = escape(title or "—")
    if username:
        return f'<a href="https://t.me/{escape(username)}">{safe_title}</a>'
    return safe_title


def create_creator_group_profile_links_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    router = Router(name="creator_group_profile_links")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    @router.callback_query(F.data.startswith("creator:group:"))
    async def group_card(callback: CallbackQuery, bot: Bot) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return

        async with session_factory() as session:
            group = (
                await session.execute(select(Group).where(Group.chat_id == chat_id))
            ).scalar_one_or_none()
            owner = (
                await session.execute(
                    select(User)
                    .join(GroupOwner, GroupOwner.user_id == User.telegram_user_id)
                    .where(GroupOwner.chat_id == chat_id, GroupOwner.is_current.is_(True))
                    .limit(1)
                )
            ).scalar_one_or_none()
            tariff_row = None
            if owner is not None:
                tariff_row = (
                    await session.execute(
                        select(Tariff, Subscription)
                        .join(Subscription, Subscription.tariff_id == Tariff.id)
                        .where(
                            Subscription.owner_user_id == owner.telegram_user_id,
                            Subscription.status == SubscriptionStatus.active.value,
                            Subscription.ends_at > datetime.now(timezone.utc),
                        )
                        .order_by(Subscription.ends_at.desc())
                        .limit(1)
                    )
                ).first()

        if group is None or callback.message is None:
            await callback.answer("Группа не найдена.", show_alert=True)
            return

        live_title = group.title
        group_username: str | None = None
        try:
            live_chat = await bot.get_chat(chat_id)
            live_title = live_chat.title or group.title
            group_username = live_chat.username
        except Exception:
            # The DB card remains available even if Telegram temporarily cannot
            # return chat metadata. Username is deliberately not cached because
            # group public links can change at any time.
            pass

        group_username_text = f"@{escape(group_username)}" if group_username else "—"
        owner_text = clickable_user_display(owner) if owner is not None else "не определён"
        if tariff_row is None:
            tariff_text = "нет активного"
        else:
            tariff, subscription = tariff_row
            tariff_text = f"{escape(tariff.name)} до {_fmt_dt(subscription.ends_at)}"

        await callback.message.edit_text(
            "👥 <b>Карточка группы</b>\n\n"
            f"Название: {_group_title(live_title, group_username)}\n"
            f"юз группы: <b>{group_username_text}</b>\n"
            f"Chat ID: <code>{group.chat_id}</code>\n"
            f"Статус: {_group_status_icon(group.status)} <b>{escape(group.status)}</b>\n"
            f"Владелец: {owner_text}\n"
            f"Тариф владельца: <b>{tariff_text}</b>\n"
            f"Добавлен бот: <b>{_fmt_dt(group.bot_added_at)}</b>\n"
            f"Подключена: <b>{_fmt_dt(group.connected_at)}</b>\n"
            f"Отключена: <b>{_fmt_dt(group.disabled_at)}</b>",
            parse_mode="HTML",
            reply_markup=_group_card_keyboard(chat_id),
            disable_web_page_preview=True,
        )
        await callback.answer()

    return router
