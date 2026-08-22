from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Subscription, SubscriptionStatus, Tariff, User
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.audit import write_audit


def _tariff_choices(user_id: int, tariffs: list[Tariff]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"💳 {tariff.code}", callback_data=f"creator:user_assign:{user_id}:{tariff.code}")]
        for tariff in tariffs
    ]
    rows.append([InlineKeyboardButton(text="◀️ Управление подпиской", callback_data=f"creator:user_sub:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_confirm(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отключить", callback_data=f"creator:user_sub_cancel_yes:{user_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"creator:user_sub:{user_id}")],
    ])


def _after_cancel(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Назначить тариф", callback_data=f"creator:user_sub_choose:{user_id}")],
        [InlineKeyboardButton(text="📋 История подписок", callback_data=f"creator:user_history:{user_id}")],
        [InlineKeyboardButton(text="◀️ Карточка пользователя", callback_data=f"creator:usercard:{user_id}")],
    ])


def create_creator_identity_privacy_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    router = Router(name="creator_identity_privacy")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    async def get_user(session: AsyncSession, user_id: int) -> User | None:
        return (await session.execute(select(User).where(User.telegram_user_id == user_id))).scalar_one_or_none()

    @router.callback_query(F.data.startswith("creator:user_sub_choose:"))
    async def choose_tariff(callback: CallbackQuery) -> None:
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
            tariffs = list((await session.execute(
                select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.id)
            )).scalars().all())
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        await callback.message.edit_text(
            "💳 <b>Выберите тариф</b>\n\n"
            f"Пользователь: {clickable_user_display(user)}\n\n"
            "Выберите тариф для назначения:",
            parse_mode="HTML",
            reply_markup=_tariff_choices(user_id, tariffs),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_sub_cancel:"))
    async def cancel_confirm(callback: CallbackQuery) -> None:
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
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        await callback.message.edit_text(
            "⚠️ <b>Отключить подписку?</b>\n\n"
            f"Пользователь: {clickable_user_display(user)}\n\n"
            "Активная подписка будет помечена как отменённая. История сохранится.",
            parse_mode="HTML",
            reply_markup=_cancel_confirm(user_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_sub_cancel_yes:"))
    async def cancel_yes(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        now = datetime.now(timezone.utc)
        cancelled_ids: list[int] = []
        async with session_factory() as session:
            async with session.begin():
                user = await get_user(session, user_id)
                if user is None:
                    await callback.answer("Пользователь не найден.", show_alert=True)
                    return
                subscriptions = list((await session.execute(
                    select(Subscription).where(
                        Subscription.owner_user_id == user_id,
                        Subscription.status == SubscriptionStatus.active.value,
                        Subscription.ends_at > now,
                    ).with_for_update()
                )).scalars().all())
                for subscription in subscriptions:
                    subscription.status = SubscriptionStatus.cancelled.value
                    cancelled_ids.append(subscription.id)
                if cancelled_ids:
                    await write_audit(
                        session,
                        "creator.user_subscription_cancelled",
                        actor_user_id=callback.from_user.id,
                        target_type="user",
                        target_id=str(user_id),
                        payload={"subscription_ids": cancelled_ids},
                    )
        if not cancelled_ids:
            await callback.answer("Активной подписки уже нет.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Подписка отключена</b>\n\n"
                f"Пользователь: {clickable_user_display(user)}\n"
                "История подписки сохранена.",
                parse_mode="HTML",
                reply_markup=_after_cancel(user_id),
            )
        await callback.answer("Подписка отключена")

    return router
