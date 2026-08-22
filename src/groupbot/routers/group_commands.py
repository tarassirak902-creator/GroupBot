from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupSettings, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group


LOCKED_TEXT = (
    "🔒 Функции Mimorus для этой группы пока недоступны.\n\n"
    "Владельцу группы нужно открыть Mimorus в личных сообщениях и активировать пробный тариф TEST."
)


def create_group_commands_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="group_commands")

    async def access_allowed(chat_id: int) -> tuple[bool, str | None]:
        async with session_factory() as session:
            group = (
                await session.execute(select(Group).where(Group.chat_id == chat_id))
            ).scalar_one_or_none()
            if group is None or group.status != GroupStatus.active.value:
                return False, "group_inactive"
            subscription = await active_subscription_for_group(session, chat_id)
            if subscription is None:
                return False, "no_tariff"
            return True, None

    async def require_access(message: Message) -> bool:
        allowed, reason = await access_allowed(message.chat.id)
        if allowed:
            return True
        if reason == "no_tariff":
            await message.answer(LOCKED_TEXT)
        elif reason == "group_inactive":
            await message.answer("⚠️ Группа не подключена или отключена владельцем.")
        return False

    @router.message(Command("help"), F.chat.type.in_({"group", "supergroup"}))
    async def help_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer(
            "❓ Помощь Mimorus\n\n"
            "👤 Для участников — профиль, активность, правила и развлечения.\n"
            "👮 Для администраторов — модерация и статистика.\n"
            "🎮 Развлечения — игры, RP, отношения, задания и рейтинги.\n\n"
            "Используйте /commands, чтобы посмотреть доступные команды."
        )

    @router.message(Command("guide"), F.chat.type.in_({"group", "supergroup"}))
    async def guide_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer(
            "📖 Как пользоваться Mimorus\n\n"
            "Участники используют команды прямо в группе.\n"
            "Администраторы выполняют модерационные действия в группе.\n"
            "Владелец настраивает группу через личные сообщения с ботом."
        )

    @router.message(Command("commands"), F.chat.type.in_({"group", "supergroup"}))
    async def commands_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer(
            "📋 Команды Mimorus\n\n"
            "/help — помощь\n"
            "/guide — как пользоваться\n"
            "/commands — все команды\n"
            "/games — игры и развлечения\n"
            "/profile — мой профиль\n"
            "/stats — моя активность\n"
            "/rules — правила группы\n"
            "/support — помощь и поддержка"
        )

    @router.message(Command("games"), F.chat.type.in_({"group", "supergroup"}))
    async def games_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer(
            "🎮 Игры и развлечения\n\n"
            "🎮 Игры\n💕 Отношения\n🎭 RP-команды\n🎯 Задания\n🏆 Рейтинги\n👤 Игровой профиль\n\n"
            "Игровые механики будут подключаться следующими функциональными блоками."
        )

    @router.message(Command("profile"), F.chat.type.in_({"group", "supergroup"}))
    async def profile_command(message: Message) -> None:
        if not await require_access(message):
            return
        if message.from_user is None:
            return
        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        await message.answer(
            "👤 Профиль\n\n"
            f"Пользователь: {username}\n"
            f"Telegram ID: {message.from_user.id}\n\n"
            "Расширенная карточка пользователя будет подключена в блоке статистики и профилей."
        )

    @router.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
    async def stats_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer("📊 Моя активность\n\nСтатистика активности будет подключена отдельным блоком.")

    @router.message(Command("rules"), F.chat.type.in_({"group", "supergroup"}))
    async def rules_command(message: Message) -> None:
        if not await require_access(message):
            return
        async with session_factory() as session:
            rules = (
                await session.execute(
                    select(GroupSettings.rules_text).where(GroupSettings.chat_id == message.chat.id)
                )
            ).scalar_one_or_none()
        await message.answer("📜 Правила группы\n\n" + (rules or "Правила группы пока не настроены владельцем."))

    @router.message(Command("support"), F.chat.type.in_({"group", "supergroup"}))
    async def support_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer(
            "🛠 Помощь и поддержка\n\n"
            "Управление обращениями находится в личных сообщениях с Mimorus в разделе «🛠 Поддержка»."
        )

    return router
