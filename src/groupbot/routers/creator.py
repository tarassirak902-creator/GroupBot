from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, Subscription, SubscriptionStatus, Tariff, User
from groupbot.services.audit import write_audit
from groupbot.services.diagnostics import rights_diagnostic


class TariffEditState(StatesGroup):
    waiting_value = State()


def _creator_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Главная", callback_data="creator:home")],
            [
                InlineKeyboardButton(text="👥 Группы", callback_data="creator:groups"),
                InlineKeyboardButton(text="👤 Пользователи", callback_data="creator:users"),
            ],
            [InlineKeyboardButton(text="💳 Тарифы и платежи", callback_data="creator:tariffs")],
            [
                InlineKeyboardButton(text="📢 Реклама", callback_data="creator:section:ads"),
                InlineKeyboardButton(text="🛠 Поддержка", callback_data="creator:section:support"),
            ],
            [
                InlineKeyboardButton(text="📣 Рассылки", callback_data="creator:section:broadcasts"),
                InlineKeyboardButton(text="🎮 Игры", callback_data="creator:section:games"),
            ],
            [
                InlineKeyboardButton(text="🔎 Диагностика", callback_data="creator:diagnostics"),
                InlineKeyboardButton(text="⚙️ Система", callback_data="creator:section:system"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _creator_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _tariffs_keyboard(tariffs: list[Tariff]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for tariff in tariffs:
        icon = "✅" if tariff.is_active else "⛔"
        rows.append([InlineKeyboardButton(text=f"{icon} {tariff.code}", callback_data=f"creator:tariff:{tariff.code}")])
    rows.append([InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tariff_keyboard(tariff: Tariff) -> InlineKeyboardMarkup:
    toggle_text = "⛔ Выключить тариф" if tariff.is_active else "✅ Включить тариф"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f"creator:tariff_toggle:{tariff.code}")],
            [InlineKeyboardButton(text="💰 Изменить цену", callback_data=f"creator:tariff_edit:{tariff.code}:price")],
            [InlineKeyboardButton(text="👤 Лимит участников", callback_data=f"creator:tariff_edit:{tariff.code}:members")],
            [InlineKeyboardButton(text="👥 Лимит групп", callback_data=f"creator:tariff_edit:{tariff.code}:groups")],
            [InlineKeyboardButton(text="⏳ Срок тарифа", callback_data=f"creator:tariff_edit:{tariff.code}:duration")],
            [InlineKeyboardButton(text="◀️ Все тарифы", callback_data="creator:tariffs")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _price_label(tariff: Tariff) -> str:
    config = tariff.limits_json or {}
    value = config.get("price_label")
    return str(value) if value else "не установлена"


def _tariff_text(tariff: Tariff) -> str:
    members = "без заданного лимита" if tariff.max_members_per_group is None else f"{tariff.max_members_per_group:,}".replace(",", " ")
    groups = "не задан" if tariff.max_groups is None else str(tariff.max_groups)
    duration = "не задан" if tariff.duration_days is None else f"{tariff.duration_days} дн."
    status = "✅ включён" if tariff.is_active else "⛔ выключен"
    return (
        f"💳 <b>{tariff.code}</b>\n\n"
        f"Статус: {status}\n"
        f"💰 Цена: <b>{_price_label(tariff)}</b>\n"
        f"👤 Участников в группе: <b>{members}</b>\n"
        f"👥 Групп: <b>{groups}</b>\n"
        f"⏳ Срок: <b>{duration}</b>\n\n"
        "Изменения сохраняются в БД и применяются без правки кода."
    )


def _group_status_icon(status: str) -> str:
    return {"active": "✅", "pending": "⏳", "disabled": "⚠️", "left": "❌"}.get(status, "•")


def _groups_keyboard(rows: list[tuple[int, str | None, str]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for chat_id, title, status in rows:
        label = f"{_group_status_icon(status)} {title or chat_id}"
        buttons.append([InlineKeyboardButton(text=label[:64], callback_data=f"creator:group:{chat_id}")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="creator:groups")])
    buttons.append([InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _group_card_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Проверить права бота", callback_data=f"creator:group_diag:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Все группы", callback_data="creator:groups")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _users_keyboard(rows: list[User]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for user in rows:
        name = user.username and f"@{user.username}" or user.first_name or str(user.telegram_user_id)
        buttons.append([InlineKeyboardButton(text=f"👤 {name}"[:64], callback_data=f"creator:user:{user.telegram_user_id}")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="creator:users")])
    buttons.append([InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def create_creator_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> Router:
    router = Router(name="creator")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    async def get_tariff(session: AsyncSession, code: str, *, lock: bool = False) -> Tariff | None:
        query = select(Tariff).where(Tariff.code == code.upper())
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).scalar_one_or_none()

    async def show_creator_home(message: Message, *, edit: bool = False) -> None:
        async with session_factory() as session:
            total_groups = (await session.execute(select(func.count()).select_from(Group))).scalar_one()
            active_groups = (
                await session.execute(select(func.count()).select_from(Group).where(Group.status == "active"))
            ).scalar_one()
            users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            active_subs = (
                await session.execute(
                    select(func.count()).select_from(Subscription).where(
                        Subscription.status == SubscriptionStatus.active.value,
                        Subscription.ends_at > datetime.now(timezone.utc),
                    )
                )
            ).scalar_one()
        text = (
            "👑 <b>Панель создателя</b>\n\n"
            f"👥 Групп в БД: <b>{total_groups}</b>\n"
            f"✅ Активных групп: <b>{active_groups}</b>\n"
            f"👤 Пользователей: <b>{users}</b>\n"
            f"💳 Активных подписок: <b>{active_subs}</b>\n\n"
            "Выберите раздел:"
        )
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=_creator_home_keyboard())
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=_creator_home_keyboard())

    async def show_tariffs(message: Message) -> None:
        async with session_factory() as session:
            tariffs = (await session.execute(select(Tariff).order_by(Tariff.id))).scalars().all()
        await message.edit_text(
            "💳 <b>Тарифы и платежи</b>\n\n"
            "Здесь создатель управляет доступностью, ценами и основными лимитами тарифов.",
            parse_mode="HTML",
            reply_markup=_tariffs_keyboard(list(tariffs)),
        )

    async def show_groups(message: Message) -> None:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Group.chat_id, Group.title, Group.status)
                    .order_by(Group.connected_at.desc().nullslast(), Group.bot_added_at.desc())
                    .limit(50)
                )
            ).all()
            total = (await session.execute(select(func.count()).select_from(Group))).scalar_one()
        text = "👥 <b>Группы</b>\n\n" f"Всего в БД: <b>{total}</b>\nПоказаны последние: <b>{len(rows)}</b>"
        if not rows:
            text += "\n\nГрупп пока нет."
        await message.edit_text(text, parse_mode="HTML", reply_markup=_groups_keyboard(list(rows)))

    async def show_users(message: Message) -> None:
        async with session_factory() as session:
            rows = (
                await session.execute(select(User).order_by(User.updated_at.desc()).limit(30))
            ).scalars().all()
            total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        text = "👤 <b>Пользователи</b>\n\n" f"Всего: <b>{total}</b>\nПоказаны последние: <b>{len(rows)}</b>"
        if not rows:
            text += "\n\nПользователей пока нет."
        await message.edit_text(text, parse_mode="HTML", reply_markup=_users_keyboard(list(rows)))

    @router.message(F.chat.type == "private", F.text == "👑 Панель создателя")
    async def creator_panel(message: Message) -> None:
        if message.from_user is None or not is_creator(message.from_user.id):
            return
        await show_creator_home(message)

    @router.callback_query(F.data == "creator:home")
    async def creator_home(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await show_creator_home(callback.message, edit=True)
        await callback.answer()

    @router.callback_query(F.data == "creator:groups")
    async def creator_groups(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await show_groups(callback.message)
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:group:"))
    async def creator_group_card(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        async with session_factory() as session:
            group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
            owner_row = (
                await session.execute(
                    select(GroupOwner.user_id, User.username, User.first_name)
                    .join(User, User.telegram_user_id == GroupOwner.user_id)
                    .where(GroupOwner.chat_id == chat_id, GroupOwner.is_current.is_(True))
                )
            ).first()
            tariff_row = None
            if owner_row is not None:
                tariff_row = (
                    await session.execute(
                        select(Tariff.name, Subscription.ends_at)
                        .join(Subscription, Subscription.tariff_id == Tariff.id)
                        .where(
                            Subscription.owner_user_id == owner_row.user_id,
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
        if owner_row is None:
            owner_text = "не определён"
        else:
            owner_name = owner_row.username and f"@{owner_row.username}" or owner_row.first_name or str(owner_row.user_id)
            owner_text = f"{owner_name} (<code>{owner_row.user_id}</code>)"
        tariff_text = f"{tariff_row.name} до {_fmt_dt(tariff_row.ends_at)}" if tariff_row else "нет активного"
        await callback.message.edit_text(
            "👥 <b>Карточка группы</b>\n\n"
            f"Название: <b>{group.title or '—'}</b>\n"
            f"Chat ID: <code>{group.chat_id}</code>\n"
            f"Статус: {_group_status_icon(group.status)} <b>{group.status}</b>\n"
            f"Владелец: {owner_text}\n"
            f"Тариф владельца: <b>{tariff_text}</b>\n"
            f"Добавлен бот: <b>{_fmt_dt(group.bot_added_at)}</b>\n"
            f"Подключена: <b>{_fmt_dt(group.connected_at)}</b>\n"
            f"Отключена: <b>{_fmt_dt(group.disabled_at)}</b>",
            parse_mode="HTML",
            reply_markup=_group_card_keyboard(chat_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:group_diag:"))
    async def creator_group_diagnostic(callback: CallbackQuery, bot: Bot) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        try:
            text, critical_ok = await rights_diagnostic(bot, chat_id)
            suffix = "\n\n✅ Критические права доступны." if critical_ok else "\n\n⚠️ Не хватает критических прав."
        except Exception as exc:
            text = f"🔎 Не удалось получить права бота для группы <code>{chat_id}</code>.\n\n<code>{type(exc).__name__}</code>"
            suffix = ""
        if callback.message is not None:
            await callback.message.edit_text(
                text + suffix,
                parse_mode="HTML",
                reply_markup=_group_card_keyboard(chat_id),
            )
        await callback.answer()

    @router.callback_query(F.data == "creator:users")
    async def creator_users(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await show_users(callback.message)
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:user:"))
    async def creator_user_card(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            user_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректный пользователь.", show_alert=True)
            return
        async with session_factory() as session:
            user = (await session.execute(select(User).where(User.telegram_user_id == user_id))).scalar_one_or_none()
            owned_count = (
                await session.execute(
                    select(func.count()).select_from(GroupOwner).where(
                        GroupOwner.user_id == user_id, GroupOwner.is_current.is_(True)
                    )
                )
            ).scalar_one()
            sub = (
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
        if user is None or callback.message is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        name = " ".join(part for part in [user.first_name, user.last_name] if part) or "—"
        username = f"@{user.username}" if user.username else "—"
        tariff_text = f"{sub.Tariff.name} до {_fmt_dt(sub.Subscription.ends_at)}" if sub else "нет активного"
        await callback.message.edit_text(
            "👤 <b>Карточка пользователя</b>\n\n"
            f"Telegram ID: <code>{user.telegram_user_id}</code>\n"
            f"Username: <b>{username}</b>\n"
            f"Имя: <b>{name}</b>\n"
            f"Telegram Premium: {'✅' if user.is_premium else '❌'}\n"
            f"Владеет группами: <b>{owned_count}</b>\n"
            f"Тариф: <b>{tariff_text}</b>\n"
            f"Первый контакт: <b>{_fmt_dt(user.first_seen_at)}</b>\n"
            f"Обновлён: <b>{_fmt_dt(user.updated_at)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Пользователи", callback_data="creator:users")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(F.data == "creator:diagnostics")
    async def creator_diagnostics(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        async with session_factory() as session:
            statuses = dict(
                (await session.execute(select(Group.status, func.count()).group_by(Group.status))).all()
            )
            total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            active_subs = (
                await session.execute(
                    select(func.count()).select_from(Subscription).where(
                        Subscription.status == SubscriptionStatus.active.value,
                        Subscription.ends_at > datetime.now(timezone.utc),
                    )
                )
            ).scalar_one()
            enabled_tariffs = (
                await session.execute(select(func.count()).select_from(Tariff).where(Tariff.is_active.is_(True)))
            ).scalar_one()
        text = (
            "🔎 <b>Глобальная диагностика</b>\n\n"
            f"✅ Активные группы: <b>{statuses.get('active', 0)}</b>\n"
            f"⏳ Ожидают подключения: <b>{statuses.get('pending', 0)}</b>\n"
            f"⚠️ Отключены: <b>{statuses.get('disabled', 0)}</b>\n"
            f"❌ Бот вышел: <b>{statuses.get('left', 0)}</b>\n"
            f"👤 Пользователей: <b>{total_users}</b>\n"
            f"💳 Активных подписок: <b>{active_subs}</b>\n"
            f"📦 Включённых тарифов: <b>{enabled_tariffs}</b>\n\n"
            "Для проверки Telegram-прав откройте конкретную группу в разделе «👥 Группы»."
        )
        if callback.message is not None:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_creator_back_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "creator:tariffs")
    async def creator_tariffs(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await show_tariffs(callback.message)
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:tariff:"))
    async def tariff_card(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        code = (callback.data or "").split(":", 2)[2].upper()
        async with session_factory() as session:
            tariff = await get_tariff(session, code)
        if tariff is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(_tariff_text(tariff), parse_mode="HTML", reply_markup=_tariff_keyboard(tariff))
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:tariff_toggle:"))
    async def tariff_toggle(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        code = (callback.data or "").split(":", 2)[2].upper()
        async with session_factory() as session:
            async with session.begin():
                tariff = await get_tariff(session, code, lock=True)
                if tariff is None:
                    await callback.answer("Тариф не найден.", show_alert=True)
                    return
                old = tariff.is_active
                tariff.is_active = not old
                await write_audit(
                    session,
                    "creator.tariff_toggled",
                    actor_user_id=callback.from_user.id,
                    target_type="tariff",
                    target_id=tariff.code,
                    payload={"old": old, "new": tariff.is_active},
                )
        if callback.message is not None:
            await callback.message.edit_text(_tariff_text(tariff), parse_mode="HTML", reply_markup=_tariff_keyboard(tariff))
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("creator:tariff_edit:"))
    async def tariff_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        code, field = parts[2].upper(), parts[3]
        prompts = {
            "price": "Отправьте новую цену/обозначение цены одной строкой. Для удаления цены отправьте: очистить",
            "members": "Отправьте максимальное число участников в одной группе. Для отсутствия заданного лимита отправьте: нет",
            "groups": "Отправьте максимальное число групп. Для отсутствия заданного лимита отправьте: нет",
            "duration": "Отправьте срок тарифа в днях. Для отсутствия фиксированного срока отправьте: нет",
        }
        if field not in prompts:
            await callback.answer("Неизвестное поле.", show_alert=True)
            return
        await state.set_state(TariffEditState.waiting_value)
        await state.update_data(code=code, field=field)
        if callback.message is not None:
            await callback.message.answer(f"✏️ <b>{code}</b>\n\n{prompts[field]}", parse_mode="HTML")
        await callback.answer()

    @router.message(TariffEditState.waiting_value, F.chat.type == "private")
    async def tariff_edit_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_creator(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        code = str(data.get("code", "")).upper()
        field = str(data.get("field", ""))
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Значение не может быть пустым.")
            return
        numeric_value: int | None = None
        if field != "price":
            if raw.casefold() not in {"нет", "очистить", "none", "—", "-"}:
                try:
                    numeric_value = int(raw)
                except ValueError:
                    await message.answer("Нужно отправить целое число или слово «нет».")
                    return
                if numeric_value <= 0:
                    await message.answer("Число должно быть больше нуля.")
                    return
        async with session_factory() as session:
            async with session.begin():
                tariff = await get_tariff(session, code, lock=True)
                if tariff is None:
                    await state.clear()
                    await message.answer("Тариф не найден.")
                    return
                old_value = None
                new_value = None
                if field == "price":
                    config = dict(tariff.limits_json or {})
                    old_value = config.get("price_label")
                    if raw.casefold() in {"очистить", "нет", "none", "—", "-"}:
                        config.pop("price_label", None)
                        new_value = None
                    else:
                        if len(raw) > 64:
                            await message.answer("Цена/обозначение слишком длинное. Максимум 64 символа.")
                            return
                        config["price_label"] = raw
                        new_value = raw
                    tariff.limits_json = config
                elif field == "members":
                    old_value = tariff.max_members_per_group
                    tariff.max_members_per_group = numeric_value
                    new_value = numeric_value
                elif field == "groups":
                    old_value = tariff.max_groups
                    tariff.max_groups = numeric_value
                    new_value = numeric_value
                elif field == "duration":
                    old_value = tariff.duration_days
                    tariff.duration_days = numeric_value
                    new_value = numeric_value
                else:
                    await state.clear()
                    await message.answer("Неизвестное поле.")
                    return
                await write_audit(
                    session,
                    "creator.tariff_updated",
                    actor_user_id=message.from_user.id,
                    target_type="tariff",
                    target_id=tariff.code,
                    payload={"field": field, "old": old_value, "new": new_value},
                )
        await state.clear()
        await message.answer(
            "✅ Изменение сохранено.\n\n" + _tariff_text(tariff),
            parse_mode="HTML",
            reply_markup=_tariff_keyboard(tariff),
        )

    @router.callback_query(F.data.startswith("creator:section:"))
    async def future_creator_section(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        labels = {
            "ads": "📢 Реклама",
            "support": "🛠 Поддержка",
            "broadcasts": "📣 Рассылки",
            "games": "🎮 Игры",
            "system": "⚙️ Система",
        }
        key = (callback.data or "").split(":", 2)[2]
        if callback.message is not None:
            await callback.message.edit_text(
                f"{labels.get(key, '👑 Раздел')}\n\nРаздел будет наполняться следующим функциональным блоком.",
                reply_markup=_creator_back_keyboard(),
            )
        await callback.answer()

    return router
