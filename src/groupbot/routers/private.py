from datetime import timezone

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, GroupStatus, Tariff
from groupbot.services.diagnostics import rights_diagnostic
from groupbot.services.groups import disable_group
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import activate_test, active_subscription_for_owner, subscription_summary
from groupbot.services.users import upsert_user
from groupbot.ui import (
    group_locked_keyboard,
    group_management_keyboard,
    owned_groups_keyboard,
    private_main_menu,
    tariff_activation_keyboard,
    tariff_back_keyboard,
    tariff_card_keyboard,
    tariff_center_keyboard,
)


SECTION_TITLES = {
    "moderation": "🛡 Модерация",
    "administration": "👮 Администрация",
    "automation": "🤖 Автоматизация",
    "statistics": "📊 Статистика",
    "games": "🎮 Настройки развлечений",
    "advertising": "📢 Реклама группы",
    "settings": "⚙️ Настройки группы",
}


NO_TARIFF_TEXT = (
    "💳 <b>Тариф не активирован</b>\n\n"
    "Для настройки подключённой группы требуется активный тариф.\n\n"
    "🎁 Вы можете начать с пробного тарифа <b>TEST на 3 дня</b> "
    "или посмотреть другие тарифы."
)


TARIFF_ICONS = {
    "TEST": "🎁",
    "BASIC": "🔹",
    "STANDARD": "🔷",
    "PRO": "💎",
    "MAX": "👑",
}


def create_private_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> Router:
    router = Router(name="private")

    async def owned_groups(user_id: int):
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Group.chat_id, Group.title, Group.status)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(GroupOwner.user_id == user_id, GroupOwner.is_current.is_(True))
                    .order_by(Group.connected_at.desc().nullslast(), Group.chat_id)
                )
            ).all()

    async def owner_has_tariff(user_id: int) -> bool:
        async with session_factory() as session:
            return await active_subscription_for_owner(session, user_id) is not None

    async def get_tariff(code: str) -> Tariff | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Tariff).where(Tariff.code == code, Tariff.is_active.is_(True))
                )
            ).scalar_one_or_none()

    async def render_tariff_center(message: Message, user_id: int, *, edit: bool = False) -> None:
        async with session_factory() as session:
            subscription, tariff = await subscription_summary(session, user_id)

        if subscription is None or tariff is None:
            status = "Активный тариф: <b>отсутствует</b>"
        else:
            ends = subscription.ends_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            status = f"Активный тариф: <b>{tariff.name}</b>\nДействует до: <b>{ends}</b>"

        text = (
            "💳 <b>Тариф и подписка</b>\n\n"
            f"{status}\n\n"
            "Выберите тариф или нужный раздел:"
        )
        markup = tariff_center_keyboard(has_active=subscription is not None)
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=markup)

    def tariff_card_text(tariff: Tariff) -> str:
        icon = TARIFF_ICONS.get(tariff.code, "💳")
        if tariff.code == "TEST":
            return (
                f"{icon} <b>TEST — 3 дня</b>\n\n"
                "Пробный тариф позволяет проверить практически все функции Mimorus "
                "с небольшими количественными лимитами.\n\n"
                "👥 Основная группа: 1\n"
                "🧪 Доп. группа: +1 только для теста сетки\n"
                "👤 Участники: без ограничения в пределах технического максимума\n"
                "🌐 Сетки: 1\n"
                "🚫 Запрещённые слова: 3\n"
                "📝 Запрещённые фразы: 3\n"
                "🔁 Автоповторы: 1\n"
                "⚖️ Свои причины: 3\n"
                "📨 Автосообщения: 1\n"
                "🏆 Собственные достижения: 1\n"
                "📤 Экспорт статистики: 2 раза за TEST\n"
                "👮 Резервный администратор: 1\n"
                "🧾 Лог-группа: 1\n"
                "🎭 Собственные VIP RP: 3\n"
                "🪪 Автоматические роли: до 3\n"
                "👑 Админ-ранги: до 3\n"
                "🛡 Расписание защиты: 1"
            )

        members = (
            f"до {tariff.max_members_per_group:,}".replace(",", " ")
            if tariff.max_members_per_group is not None
            else "настраивается"
        )
        groups = str(tariff.max_groups) if tariff.max_groups is not None else "настраивается"
        return (
            f"{icon} <b>{tariff.name}</b>\n\n"
            f"👤 Участников в одной группе: <b>{members}</b>\n"
            f"👥 Групп: <b>{groups}</b>\n\n"
            "Основные функции Mimorus доступны; тарифы отличаются масштабом "
            "и количественными лимитами.\n"
            "📢 Рекламные функции доступны на платных тарифах.\n\n"
            "💰 Стоимость пока не установлена владельцем Mimorus."
        )

    async def render_group(callback: CallbackQuery, chat_id: int) -> None:
        if callback.from_user is None or callback.message is None:
            return
        async with session_factory() as session:
            if not await is_group_owner(session, chat_id, callback.from_user.id):
                await callback.answer("Эта группа вам не принадлежит.", show_alert=True)
                return
            group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
            subscription = await active_subscription_for_owner(session, callback.from_user.id)
        if group is None:
            await callback.answer("Группа не найдена.", show_alert=True)
            return

        labels = {
            GroupStatus.active.value: "✅ Подключена",
            GroupStatus.pending.value: "⏳ Ожидает подключения",
            GroupStatus.disabled.value: "⚠️ Отключена",
            GroupStatus.left.value: "❌ Бот покинул группу",
        }
        if subscription is None:
            await callback.message.edit_text(
                f"⚙️ <b>{group.title or group.chat_id}</b>\n\n"
                f"Статус группы: {labels.get(group.status, group.status)}\n"
                "Тариф: ❌ не активирован\n\n"
                "🔒 Для настройки данной группы необходимо сначала активировать тариф.\n\n"
                "🎁 Вы можете активировать пробный тариф TEST на 3 дня.",
                parse_mode="HTML",
                reply_markup=group_locked_keyboard(group.chat_id),
            )
        else:
            await callback.message.edit_text(
                f"⚙️ Управление группой\n\n{group.title or group.chat_id}\n"
                f"Статус: {labels.get(group.status, group.status)}\n"
                "Тариф: ✅ активен",
                reply_markup=group_management_keyboard(
                    group.chat_id,
                    active=group.status == GroupStatus.active.value,
                ),
            )
        await callback.answer()

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            async with session.begin():
                await upsert_user(session, message.from_user)
        await message.answer(
            "🏠 Главное меню",
            reply_markup=private_main_menu(is_creator=message.from_user.id in settings.creator_id_set),
        )

    @router.message(F.chat.type == "private", F.text == "💳 Тариф и подписка")
    async def tariff_menu(message: Message) -> None:
        if message.from_user is None:
            return
        await render_tariff_center(message, message.from_user.id)

    @router.callback_query(F.data == "tariff:show")
    async def tariff_show_callback(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await render_tariff_center(callback.message, callback.from_user.id, edit=True)
        await callback.answer()

    @router.callback_query(F.data.startswith("tariff:card:"))
    async def tariff_card(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        code = (callback.data or "").split(":", 2)[2].upper()
        tariff = await get_tariff(code)
        if tariff is None:
            await callback.answer("Тариф сейчас недоступен.", show_alert=True)
            return
        async with session_factory() as session:
            active = await active_subscription_for_owner(session, callback.from_user.id)
            previous_trial = None
            if code == "TEST":
                from groupbot.models import Subscription
                previous_trial = (
                    await session.execute(
                        select(Subscription.id).where(
                            Subscription.owner_user_id == callback.from_user.id,
                            Subscription.is_trial.is_(True),
                        ).limit(1)
                    )
                ).scalar_one_or_none()
        can_activate_test = code == "TEST" and active is None and previous_trial is None
        await callback.message.edit_text(
            tariff_card_text(tariff),
            parse_mode="HTML",
            reply_markup=tariff_card_keyboard(code, can_activate_test=can_activate_test),
        )
        await callback.answer()

    @router.callback_query(F.data == "tariff:activate_test")
    async def activate_test_callback(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        async with session_factory() as session:
            async with session.begin():
                subscription, result = await activate_test(session, callback.from_user.id)
        if result == "trial_already_used":
            await callback.answer("Пробный тариф TEST уже использован.", show_alert=True)
            return
        if result == "trial_unavailable":
            await callback.answer("TEST сейчас недоступен.", show_alert=True)
            return
        if result == "already_active":
            await callback.answer("У вас уже есть активный тариф.", show_alert=True)
            return

        ends = subscription.ends_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if subscription else "—"
        await callback.message.edit_text(
            "🎉 <b>Пробный тариф TEST активирован!</b>\n\n"
            "⏳ Срок: 3 дня\n"
            f"📅 Действует до: <b>{ends}</b>\n\n"
            "✅ Настройки подключённых групп и групповые команды Mimorus теперь доступны.",
            parse_mode="HTML",
            reply_markup=tariff_back_keyboard(),
        )
        await callback.answer("TEST активирован")

    @router.callback_query(F.data.startswith("tariff:choose:"))
    async def choose_paid_tariff(callback: CallbackQuery) -> None:
        code = (callback.data or "").split(":", 2)[2].upper()
        tariff = await get_tariff(code)
        if tariff is None or code == "TEST":
            await callback.answer("Тариф сейчас недоступен.", show_alert=True)
            return
        await callback.answer(
            "Стоимость тарифа пока не установлена владельцем Mimorus. Покупка временно недоступна.",
            show_alert=True,
        )

    @router.callback_query(F.data == "tariff:subscription")
    async def my_subscription(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        async with session_factory() as session:
            subscription, tariff = await subscription_summary(session, callback.from_user.id)
        if subscription is None or tariff is None:
            await callback.message.edit_text(
                NO_TARIFF_TEXT,
                parse_mode="HTML",
                reply_markup=tariff_activation_keyboard(),
            )
        else:
            ends = subscription.ends_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            await callback.message.edit_text(
                "📜 <b>Моя подписка</b>\n\n"
                f"Тариф: <b>{tariff.name}</b>\n"
                "Статус: ✅ активен\n"
                f"Действует до: <b>{ends}</b>",
                parse_mode="HTML",
                reply_markup=tariff_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "tariff:custom")
    async def custom_tariff(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "🛠 <b>Собрать свой тариф</b>\n\n"
                "Индивидуальный тариф позволяет выбрать нужный лимит участников, "
                "количество групп, модули и количественные лимиты.\n\n"
                "💰 Цена рассчитывается динамически. Формула цены пока не утверждена, "
                "поэтому Mimorus не будет придумывать её автоматически.",
                parse_mode="HTML",
                reply_markup=tariff_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "tariff:addons")
    async def tariff_addons(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "📦 <b>Дополнительные покупки</b>\n\n"
                "Отдельно могут докупаться количественные лимиты:\n"
                "• дополнительные группы;\n"
                "• запрещённые слова и фразы;\n"
                "• автоповторы;\n"
                "• свои причины;\n"
                "• шаблоны;\n"
                "• сетки;\n"
                "• экспорт и другие разрешённые лимиты.\n\n"
                "Пакет действует до конца основной подписки. Конкретные цены пока не утверждены.",
                parse_mode="HTML",
                reply_markup=tariff_back_keyboard(),
            )
        await callback.answer()

    @router.message(F.chat.type == "private", F.text == "👥 Мои группы")
    async def my_groups(message: Message) -> None:
        if message.from_user is None:
            return
        rows = await owned_groups(message.from_user.id)
        if not rows:
            await message.answer("👥 У вас пока нет подключённых групп.")
            return
        await message.answer("👥 Мои группы\nВыберите группу:", reply_markup=owned_groups_keyboard(rows))

    @router.callback_query(F.data == "group:list")
    async def group_list_callback(callback: CallbackQuery) -> None:
        rows = await owned_groups(callback.from_user.id)
        if callback.message is not None:
            if rows:
                await callback.message.edit_text("👥 Мои группы\nВыберите группу:", reply_markup=owned_groups_keyboard(rows))
            else:
                await callback.message.edit_text("👥 У вас пока нет подключённых групп.")
        await callback.answer()

    @router.callback_query(F.data.startswith("group:open:"))
    async def open_group(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        await render_group(callback, chat_id)

    @router.callback_query(F.data.startswith("group:diagnostic:"))
    async def diagnostic(callback: CallbackQuery, bot: Bot) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        async with session_factory() as session:
            if not await is_group_owner(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
        try:
            text, critical_ok = await rights_diagnostic(bot, chat_id)
        except Exception:
            await callback.answer("Не удалось получить права бота в группе.", show_alert=True)
            return
        suffix = "\n\n✅ Критические права доступны." if critical_ok else "\n\n⚠️ Не хватает критических прав: часть функций будет недоступна."
        if callback.message is not None:
            await callback.message.edit_text(text + suffix, reply_markup=group_management_keyboard(chat_id, active=True))
        await callback.answer()

    @router.callback_query(F.data.startswith("group:disable:"))
    async def disable(callback: CallbackQuery, bot: Bot) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await is_group_owner(session, chat_id, callback.from_user.id):
                    await callback.answer("Отключить группу может только владелец.", show_alert=True)
                    return
                await disable_group(session, chat_id, callback.from_user.id)
        text = (
            "⚠️ Бот отключён владельцем от данной группы.\n"
            "Функции модерации, статистики, игр и автоматизации больше не активны.\n"
            "Если бот не будет подключён повторно в течение 2 минут, он покинет группу."
        )
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            pass
        if callback.message is not None:
            await callback.message.edit_text(text, reply_markup=group_management_keyboard(chat_id, active=False))
        await callback.answer("Группа отключена.")

    @router.callback_query(F.data.startswith("group:reconnect:"))
    async def reconnect_info(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.answer("Чтобы восстановить группу, её фактический владелец должен написать в группе: подключить")
        await callback.answer()

    @router.callback_query(F.data.startswith("group:section:"))
    async def section(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректный раздел.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        section_key = parts[3]
        async with session_factory() as session:
            if not await is_group_owner(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            if await active_subscription_for_owner(session, callback.from_user.id) is None:
                await callback.answer("Сначала активируйте тариф.", show_alert=True)
                return
        title = SECTION_TITLES.get(section_key, "⚙️ Раздел")
        if callback.message is not None:
            await callback.message.edit_text(
                f"{title}\n\nРаздел подключён к новой архитектуре и будет наполняться следующими функциональными блоками.",
                reply_markup=group_management_keyboard(chat_id, active=True),
            )
        await callback.answer()

    @router.callback_query(F.data == "nav:home")
    async def home_callback(callback: CallbackQuery) -> None:
        if callback.message is not None:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(
                "🏠 Главное меню",
                reply_markup=private_main_menu(is_creator=callback.from_user.id in settings.creator_id_set),
            )
        await callback.answer()

    @router.message(F.chat.type == "private", F.text == "👤 Мой аккаунт")
    async def my_account(message: Message) -> None:
        if message.from_user is None:
            return
        tariff_state = "✅ активен" if await owner_has_tariff(message.from_user.id) else "❌ не активирован"
        await message.answer(
            f"👤 Мой аккаунт\nTelegram ID: {message.from_user.id}\n"
            f"Имя: {message.from_user.full_name}\nТариф: {tariff_state}"
        )

    @router.message(F.chat.type == "private", F.text.in_({"🌐 Сетки групп", "📢 Реклама", "🛠 Поддержка", "👑 Панель создателя"}))
    async def future_section(message: Message) -> None:
        if message.text == "👑 Панель создателя" and (
            message.from_user is None or message.from_user.id not in settings.creator_id_set
        ):
            return
        await message.answer(f"{message.text}\n\nРаздел появится в следующем крупном этапе разработки.")

    return router
