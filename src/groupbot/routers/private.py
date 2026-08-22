from datetime import timezone

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.diagnostics import rights_diagnostic
from groupbot.services.groups import disable_group
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import (
    activate_test,
    active_subscription_for_owner,
    subscription_summary,
)
from groupbot.services.users import upsert_user
from groupbot.ui import (
    group_locked_keyboard,
    group_management_keyboard,
    owned_groups_keyboard,
    private_main_menu,
    tariff_activation_keyboard,
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
    "Группа уже подключена к Mimorus, но функции управления и команды в группе "
    "станут доступны после активации тарифа.\n\n"
    "🎁 Для начала доступен пробный тариф <b>TEST на 3 дня</b>."
)


def create_private_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> Router:
    router = Router(name="private")

    async def owned_groups(user_id: int):
        async with session_factory() as session:
            return (await session.execute(
                select(Group.chat_id, Group.title, Group.status)
                .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                .where(GroupOwner.user_id == user_id, GroupOwner.is_current.is_(True))
                .order_by(Group.connected_at.desc().nullslast(), Group.chat_id)
            )).all()

    async def has_owned_group(user_id: int) -> bool:
        async with session_factory() as session:
            value = (await session.execute(
                select(GroupOwner.id).where(
                    GroupOwner.user_id == user_id,
                    GroupOwner.is_current.is_(True),
                ).limit(1)
            )).scalar_one_or_none()
            return value is not None

    async def owner_has_tariff(user_id: int) -> bool:
        async with session_factory() as session:
            return await active_subscription_for_owner(session, user_id) is not None

    async def show_tariff(message: Message, user_id: int) -> None:
        async with session_factory() as session:
            subscription, tariff = await subscription_summary(session, user_id)
        if subscription is None or tariff is None:
            await message.answer(
                NO_TARIFF_TEXT,
                parse_mode="HTML",
                reply_markup=tariff_activation_keyboard(),
            )
            return
        ends = subscription.ends_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        await message.answer(
            "💳 <b>Тариф и подписка</b>\n\n"
            f"Тариф: <b>{tariff.name}</b>\n"
            f"Статус: ✅ активен\n"
            f"Действует до: <b>{ends}</b>",
            parse_mode="HTML",
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
                f"⚙️ Управление группой\n\n{group.title or group.chat_id}\n"
                f"Статус: {labels.get(group.status, group.status)}\n"
                "Тариф: ❌ не активирован\n\n"
                "Сначала активируйте TEST, чтобы открыть управление группой и групповые команды.",
                reply_markup=group_locked_keyboard(group.chat_id),
            )
        else:
            await callback.message.edit_text(
                f"⚙️ Управление группой\n\n{group.title or group.chat_id}\n"
                f"Статус: {labels.get(group.status, group.status)}\n"
                "Тариф: ✅ активен",
                reply_markup=group_management_keyboard(group.chat_id, active=group.status == GroupStatus.active.value),
            )
        await callback.answer()

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            async with session.begin():
                await upsert_user(session, message.from_user)
        if await has_owned_group(message.from_user.id) and not await owner_has_tariff(message.from_user.id):
            await message.answer("Подключение группы завершено.", reply_markup=ReplyKeyboardRemove())
            await show_tariff(message, message.from_user.id)
            return
        await message.answer(
            "🏠 Главное меню",
            reply_markup=private_main_menu(is_creator=message.from_user.id in settings.creator_id_set),
        )

    @router.message(F.chat.type == "private", F.text == "💳 Тариф и подписка")
    async def tariff_menu(message: Message) -> None:
        if message.from_user is None:
            return
        await show_tariff(message, message.from_user.id)

    @router.callback_query(F.data == "tariff:show")
    async def tariff_show_callback(callback: CallbackQuery) -> None:
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
                "💳 <b>Тариф и подписка</b>\n\n"
                f"Тариф: <b>{tariff.name}</b>\nСтатус: ✅ активен\nДействует до: <b>{ends}</b>",
                parse_mode="HTML",
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
            "✅ Управление группой и команды Mimorus в группе теперь доступны.",
            parse_mode="HTML",
        )
        await callback.message.answer(
            "🏠 Главное меню",
            reply_markup=private_main_menu(is_creator=callback.from_user.id in settings.creator_id_set),
        )
        await callback.answer("TEST активирован")

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
            await callback.message.delete()
            if await owner_has_tariff(callback.from_user.id) or not await has_owned_group(callback.from_user.id):
                await callback.message.answer(
                    "🏠 Главное меню",
                    reply_markup=private_main_menu(is_creator=callback.from_user.id in settings.creator_id_set),
                )
            else:
                await callback.message.answer(NO_TARIFF_TEXT, parse_mode="HTML", reply_markup=tariff_activation_keyboard())
        await callback.answer()

    @router.message(F.chat.type == "private", F.text == "👤 Мой аккаунт")
    async def my_account(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(f"👤 Мой аккаунт\nTelegram ID: {message.from_user.id}\nИмя: {message.from_user.full_name}")

    @router.message(F.chat.type == "private", F.text.in_({"🌐 Сетки групп", "📢 Реклама", "🛠 Поддержка", "👑 Панель создателя"}))
    async def future_section(message: Message) -> None:
        if message.text == "👑 Панель создателя" and (message.from_user is None or message.from_user.id not in settings.creator_id_set):
            return
        if message.from_user is not None and await has_owned_group(message.from_user.id) and not await owner_has_tariff(message.from_user.id):
            await message.answer(NO_TARIFF_TEXT, parse_mode="HTML", reply_markup=tariff_activation_keyboard())
            return
        await message.answer(f"{message.text}\n\nРаздел появится в следующем крупном этапе разработки.")

    return router
