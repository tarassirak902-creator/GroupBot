from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.users import upsert_user
from groupbot.ui import private_main_menu


def create_private_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> Router:
    router = Router(name="private")

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

    @router.message(F.chat.type == "private", F.text == "👥 Мои группы")
    async def my_groups(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            rows = (await session.execute(
                select(Group.chat_id, Group.title, Group.status)
                .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                .where(GroupOwner.user_id == message.from_user.id, GroupOwner.is_current.is_(True))
                .order_by(Group.connected_at.desc().nullslast(), Group.chat_id)
            )).all()
        if not rows:
            await message.answer("👥 У вас пока нет подключённых групп.")
            return
        status_icon = {
            GroupStatus.active.value: "✅",
            GroupStatus.pending.value: "⏳",
            GroupStatus.disabled.value: "⚠️",
            GroupStatus.left.value: "❌",
        }
        lines = ["👥 Мои группы"]
        for chat_id, title, status in rows:
            lines.append(f"{status_icon.get(status, '•')} {title or chat_id}")
        await message.answer("\n".join(lines))

    @router.message(F.chat.type == "private", F.text == "👤 Мой аккаунт")
    async def my_account(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "👤 Мой аккаунт\n"
            f"Telegram ID: {message.from_user.id}\n"
            f"Имя: {message.from_user.full_name}"
        )

    return router
