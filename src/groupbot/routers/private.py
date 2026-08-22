from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.audit import write_audit
from groupbot.services.diagnostics import rights_diagnostic
from groupbot.services.groups import disable_group
from groupbot.services.permissions import is_group_owner
from groupbot.services.users import upsert_user
from groupbot.ui import group_management_keyboard, owned_groups_keyboard, private_main_menu


SECTION_TITLES = {
    "moderation": "🛡 Модерация",
    "administration": "👮 Администрация",
    "automation": "🤖 Автоматизация",
    "statistics": "📊 Статистика",
    "games": "🎮 Настройки развлечений",
    "advertising": "📢 Реклама группы",
    "settings": "⚙️ Настройки группы",
}


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

    async def render_group(callback: CallbackQuery, chat_id: int) -> None:
        if callback.from_user is None or callback.message is None:
            return
        async with session_factory() as session:
            if not await is_group_owner(session, chat_id, callback.from_user.id):
                await callback.answer("Эта группа вам не принадлежит.", show_alert=True)
                return
            group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
        if group is None:
            await callback.answer("Группа не найдена.", show_alert=True)
            return
        labels = {
            GroupStatus.active.value: "✅ Подключена",
            GroupStatus.pending.value: "⏳ Ожидает подключения",
            GroupStatus.disabled.value: "⚠️ Отключена",
            GroupStatus.left.value: "❌ Бот покинул группу",
        }
        await callback.message.edit_text(
            f"⚙️ Управление группой\n\n{group.title or group.chat_id}\nСтатус: {labels.get(group.status, group.status)}",
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
        await message.answer("🏠 Главное меню", reply_markup=private_main_menu(is_creator=message.from_user.id in settings.creator_id_set))

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
            await callback.answer("Некорректная группа.", show_alert=True); return
        await render_group(callback, chat_id)

    @router.callback_query(F.data.startswith("group:diagnostic:"))
    async def diagnostic(callback: CallbackQuery, bot: Bot) -> None:
        try: chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError): await callback.answer("Некорректная группа.", show_alert=True); return
        async with session_factory() as session:
            if not await is_group_owner(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True); return
        try:
            text, critical_ok = await rights_diagnostic(bot, chat_id)
        except Exception:
            await callback.answer("Не удалось получить права бота в группе.", show_alert=True); return
        suffix = "\n\n✅ Критические права доступны." if critical_ok else "\n\n⚠️ Не хватает критических прав: часть функций будет недоступна."
        if callback.message is not None:
            await callback.message.edit_text(text + suffix, reply_markup=group_management_keyboard(chat_id, active=True))
        await callback.answer()

    @router.callback_query(F.data.startswith("group:disable:"))
    async def disable(callback: CallbackQuery, bot: Bot) -> None:
        try: chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError): await callback.answer("Некорректная группа.", show_alert=True); return
        async with session_factory() as session:
            async with session.begin():
                if not await is_group_owner(session, chat_id, callback.from_user.id):
                    await callback.answer("Отключить группу может только владелец.", show_alert=True); return
                await disable_group(session, chat_id, callback.from_user.id)
        text = (
            "⚠️ Бот отключён владельцем от данной группы.\n"
            "Функции модерации, статистики, игр и автоматизации больше не активны.\n"
            "Если бот не будет подключён повторно в течение 2 минут, он покинет группу."
        )
        try: await bot.send_message(chat_id, text)
        except Exception: pass
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
            await callback.answer("Некорректный раздел.", show_alert=True); return
        try: chat_id = int(parts[2])
        except ValueError: await callback.answer("Некорректная группа.", show_alert=True); return
        section_key = parts[3]
        async with session_factory() as session:
            if not await is_group_owner(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True); return
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
            await callback.message.answer("🏠 Главное меню", reply_markup=private_main_menu(is_creator=callback.from_user.id in settings.creator_id_set))
        await callback.answer()

    @router.message(F.chat.type == "private", F.text == "👤 Мой аккаунт")
    async def my_account(message: Message) -> None:
        if message.from_user is None: return
        await message.answer(f"👤 Мой аккаунт\nTelegram ID: {message.from_user.id}\nИмя: {message.from_user.full_name}")

    @router.message(F.chat.type == "private", F.text.in_({"🌐 Сетки групп", "📢 Реклама", "💳 Тариф и подписка", "🛠 Поддержка", "👑 Панель создателя"}))
    async def future_section(message: Message) -> None:
        if message.text == "👑 Панель создателя" and (message.from_user is None or message.from_user.id not in settings.creator_id_set):
            return
        await message.answer(f"{message.text}\n\nРаздел появится в следующем крупном этапе разработки.")

    return router
