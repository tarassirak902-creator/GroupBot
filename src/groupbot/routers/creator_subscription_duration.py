from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Subscription, SubscriptionStatus, Tariff, User
from groupbot.services.audit import write_audit


class CustomDurationState(StatesGroup):
    waiting_days = State()


def _duration_keyboard(user_id: int, tariff_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="7 дней",
                    callback_data=f"creator:user_assign_days:{user_id}:{tariff_code}:7",
                ),
                InlineKeyboardButton(
                    text="15 дней",
                    callback_data=f"creator:user_assign_days:{user_id}:{tariff_code}:15",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="1 месяц",
                    callback_data=f"creator:user_assign_days:{user_id}:{tariff_code}:30",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Другой срок",
                    callback_data=f"creator:user_assign_custom:{user_id}:{tariff_code}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Управление подпиской",
                    callback_data=f"creator:user_sub:{user_id}",
                )
            ],
        ]
    )


def _subscription_keyboard(user_id: int, *, has_active: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_active:
        rows.extend(
            [
                [InlineKeyboardButton(text="🔄 Сменить тариф", callback_data=f"creator:user_sub_choose:{user_id}")],
                [InlineKeyboardButton(text="⛔ Отключить подписку", callback_data=f"creator:user_sub_cancel:{user_id}")],
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="🎁 Назначить тариф", callback_data=f"creator:user_sub_choose:{user_id}")])
    rows.extend(
        [
            [InlineKeyboardButton(text="📋 История подписок", callback_data=f"creator:user_history:{user_id}")],
            [InlineKeyboardButton(text="◀️ Карточка пользователя", callback_data=f"creator:usercard:{user_id}")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _user_display(user: User) -> str:
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    username = f"@{user.username}" if user.username else ""
    if full_name and username:
        return f"{full_name} | {username}"
    if full_name:
        return full_name
    if username:
        return username
    return "Пользователь"


def create_creator_subscription_duration_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    router = Router(name="creator_subscription_duration")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    async def get_user(user_id: int) -> User | None:
        async with session_factory() as session:
            return (
                await session.execute(select(User).where(User.telegram_user_id == user_id))
            ).scalar_one_or_none()

    async def get_active_subscription(user_id: int) -> tuple[Subscription, Tariff] | None:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Subscription, Tariff)
                    .join(Tariff, Tariff.id == Subscription.tariff_id)
                    .where(
                        Subscription.owner_user_id == user_id,
                        Subscription.status == SubscriptionStatus.active.value,
                        Subscription.ends_at > now,
                    )
                    .order_by(Subscription.ends_at.desc())
                    .limit(1)
                )
            ).first()

    async def assign_subscription(
        *,
        creator_id: int,
        user_id: int,
        tariff_code: str,
        duration_days: int,
    ) -> tuple[Subscription, Tariff, User] | None:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            async with session.begin():
                user = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == user_id)
                    )
                ).scalar_one_or_none()
                tariff = (
                    await session.execute(
                        select(Tariff)
                        .where(Tariff.code == tariff_code.upper())
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if user is None or tariff is None or not tariff.is_active:
                    return None

                active_rows = (
                    await session.execute(
                        select(Subscription)
                        .where(
                            Subscription.owner_user_id == user_id,
                            Subscription.status == SubscriptionStatus.active.value,
                            Subscription.ends_at > now,
                        )
                        .with_for_update()
                    )
                ).scalars().all()

                replaced: list[int] = []
                for old in active_rows:
                    old.status = SubscriptionStatus.cancelled.value
                    replaced.append(old.id)

                subscription = Subscription(
                    owner_user_id=user_id,
                    tariff_id=tariff.id,
                    status=SubscriptionStatus.active.value,
                    started_at=now,
                    ends_at=now + timedelta(days=duration_days),
                    is_trial=tariff.is_trial,
                )
                session.add(subscription)
                await session.flush()
                await write_audit(
                    session,
                    "creator.user_subscription_assigned",
                    actor_user_id=creator_id,
                    target_type="user",
                    target_id=str(user_id),
                    payload={
                        "subscription_id": subscription.id,
                        "tariff": tariff.code,
                        "duration_days": duration_days,
                        "replaced_subscription_ids": replaced,
                        "duration_source": "preset_or_creator_input",
                        "access_activated": True,
                    },
                )
                return subscription, tariff, user

    async def show_success(
        callback_or_message,
        *,
        user: User,
        user_id: int,
        tariff_code: str,
        days: int,
        subscription: Subscription,
        edit: bool,
    ) -> None:
        text = (
            "✅ <b>Подписка назначена</b>\n\n"
            f"Пользователь: <b>{_user_display(user)}</b>\n"
            f"Тариф: <b>{tariff_code}</b>\n"
            f"Срок: <b>{days} дн.</b>\n"
            f"Действует до: <b>{_fmt_dt(subscription.ends_at)}</b>\n"
            "Доступ к функциям: <b>✅ активирован</b>"
        )
        if edit:
            await callback_or_message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_subscription_keyboard(user_id),
            )
        else:
            await callback_or_message.answer(
                text,
                parse_mode="HTML",
                reply_markup=_subscription_keyboard(user_id),
            )

    @router.callback_query(F.data.startswith("creator:user_sub:"))
    async def subscription_screen(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return

        user = await get_user(user_id)
        if user is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        active = await get_active_subscription(user_id)

        if active is None:
            text = (
                "💳 <b>Управление подпиской</b>\n\n"
                f"Пользователь: <b>{_user_display(user)}</b>\n"
                "Активная подписка: <b>нет</b>\n"
                "Доступ к функциям: <b>❌ не активирован</b>"
            )
        else:
            subscription, tariff = active
            if tariff.code == "TEST":
                access_line = "Пробный TEST: <b>✅ активирован</b>"
            else:
                access_line = f"TEST-доступ: <b>✅ активирован тарифом {tariff.code}</b>"
            text = (
                "💳 <b>Управление подпиской</b>\n\n"
                f"Пользователь: <b>{_user_display(user)}</b>\n"
                f"Тариф: <b>{tariff.name}</b>\n"
                f"Начало: <b>{_fmt_dt(subscription.started_at)}</b>\n"
                f"Окончание: <b>{_fmt_dt(subscription.ends_at)}</b>\n"
                f"{access_line}\n"
                "Доступ к функциям: <b>✅ активирован</b>"
            )

        if callback.message is not None:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_subscription_keyboard(user_id, has_active=active is not None),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_assign:"))
    async def choose_tariff(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        try:
            user_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        tariff_code = parts[3].upper()

        async with session_factory() as session:
            tariff = (
                await session.execute(
                    select(Tariff).where(Tariff.code == tariff_code)
                )
            ).scalar_one_or_none()
            user = (
                await session.execute(select(User).where(User.telegram_user_id == user_id))
            ).scalar_one_or_none()

        if tariff is None or not tariff.is_active or user is None:
            await callback.answer("Тариф или пользователь недоступен.", show_alert=True)
            return

        if tariff.code == "TEST":
            days = tariff.duration_days or 3
            result = await assign_subscription(
                creator_id=callback.from_user.id,
                user_id=user_id,
                tariff_code=tariff.code,
                duration_days=days,
            )
            if result is None:
                await callback.answer("Не удалось назначить тариф.", show_alert=True)
                return
            subscription, _, assigned_user = result
            if callback.message is not None:
                await show_success(
                    callback.message,
                    user=assigned_user,
                    user_id=user_id,
                    tariff_code=tariff.code,
                    days=days,
                    subscription=subscription,
                    edit=True,
                )
            await callback.answer("Сохранено")
            return

        if callback.message is not None:
            await callback.message.edit_text(
                f"⏳ <b>{tariff.code}</b>\n\n"
                f"Пользователь: <b>{_user_display(user)}</b>\n\n"
                "Выберите срок доступа:",
                parse_mode="HTML",
                reply_markup=_duration_keyboard(user_id, tariff.code),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user_assign_days:"))
    async def choose_preset_duration(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        try:
            user_id = int(parts[2])
            days = int(parts[4])
        except ValueError:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        tariff_code = parts[3].upper()
        if days not in {7, 15, 30} or tariff_code == "TEST":
            await callback.answer("Этот срок недоступен.", show_alert=True)
            return

        result = await assign_subscription(
            creator_id=callback.from_user.id,
            user_id=user_id,
            tariff_code=tariff_code,
            duration_days=days,
        )
        if result is None:
            await callback.answer("Не удалось назначить тариф.", show_alert=True)
            return
        subscription, tariff, user = result
        if callback.message is not None:
            await show_success(
                callback.message,
                user=user,
                user_id=user_id,
                tariff_code=tariff.code,
                days=days,
                subscription=subscription,
                edit=True,
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("creator:user_assign_custom:"))
    async def custom_duration_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        try:
            user_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        tariff_code = parts[3].upper()
        if tariff_code == "TEST":
            await callback.answer("TEST выдаётся только на 3 дня.", show_alert=True)
            return

        user = await get_user(user_id)
        if user is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return

        await state.set_state(CustomDurationState.waiting_days)
        await state.update_data(user_id=user_id, tariff_code=tariff_code)
        if callback.message is not None:
            await callback.message.answer(
                f"✏️ <b>{tariff_code}: другой срок</b>\n\n"
                f"Пользователь: <b>{_user_display(user)}</b>\n"
                "Отправьте количество дней целым числом.",
                parse_mode="HTML",
            )
        await callback.answer()

    @router.message(CustomDurationState.waiting_days, F.chat.type == "private")
    async def custom_duration_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_creator(message.from_user.id):
            await state.clear()
            return
        try:
            days = int((message.text or "").strip())
        except ValueError:
            await message.answer("Отправьте срок целым числом дней.")
            return
        if days <= 0:
            await message.answer("Срок должен быть больше нуля дней.")
            return

        data = await state.get_data()
        user_id = int(data.get("user_id"))
        tariff_code = str(data.get("tariff_code", "")).upper()
        result = await assign_subscription(
            creator_id=message.from_user.id,
            user_id=user_id,
            tariff_code=tariff_code,
            duration_days=days,
        )
        await state.clear()
        if result is None:
            await message.answer("Не удалось назначить подписку.")
            return
        subscription, tariff, user = result
        await show_success(
            message,
            user=user,
            user_id=user_id,
            tariff_code=tariff.code,
            days=days,
            subscription=subscription,
            edit=False,
        )

    return router
