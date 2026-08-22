from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Tariff
from groupbot.services.audit import write_audit


class TariffEditState(StatesGroup):
    waiting_value = State()


def _creator_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Главная", callback_data="creator:home")],
            [InlineKeyboardButton(text="👥 Группы", callback_data="creator:section:groups"), InlineKeyboardButton(text="👤 Пользователи", callback_data="creator:section:users")],
            [InlineKeyboardButton(text="💳 Тарифы и платежи", callback_data="creator:tariffs")],
            [InlineKeyboardButton(text="📢 Реклама", callback_data="creator:section:ads"), InlineKeyboardButton(text="🛠 Поддержка", callback_data="creator:section:support")],
            [InlineKeyboardButton(text="📣 Рассылки", callback_data="creator:section:broadcasts"), InlineKeyboardButton(text="🎮 Игры", callback_data="creator:section:games")],
            [InlineKeyboardButton(text="🔎 Диагностика", callback_data="creator:section:diagnostics"), InlineKeyboardButton(text="⚙️ Система", callback_data="creator:section:system")],
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
        text = (
            "👑 <b>Панель создателя</b>\n\n"
            "Глобальное управление Mimorus. Выберите раздел:"
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
            await callback.message.edit_text(
                _tariff_text(tariff),
                parse_mode="HTML",
                reply_markup=_tariff_keyboard(tariff),
            )
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
            "price": "Отправьте новую цену/обозначение цены одной строкой. Например значение, которое вы хотите показывать пользователям. Для удаления цены отправьте: очистить",
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
            "groups": "👥 Группы",
            "users": "👤 Пользователи",
            "ads": "📢 Реклама",
            "support": "🛠 Поддержка",
            "broadcasts": "📣 Рассылки",
            "games": "🎮 Игры",
            "diagnostics": "🔎 Диагностика",
            "system": "⚙️ Система",
        }
        key = (callback.data or "").split(":", 2)[2]
        if callback.message is not None:
            await callback.message.edit_text(
                f"{labels.get(key, '👑 Раздел')}\n\nРаздел будет наполняться следующим функциональным блоком.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")]]),
            )
        await callback.answer()

    return router
