from aiogram import Bot, F, Router
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.services.groups import connect_group, register_pending_group


def _mark(value: bool | None) -> str:
    return "🟢" if bool(value) else "🔴"


async def rights_diagnostic(bot: Bot, chat_id: int) -> str:
    me = await bot.get_me()
    member = await bot.get_chat_member(chat_id, me.id)
    is_admin = member.status == "administrator"
    return (
        "🔎 Диагностика\n"
        f"{_mark(is_admin and getattr(member, 'can_delete_messages', False))} Удаление сообщений\n"
        f"{_mark(is_admin and getattr(member, 'can_restrict_members', False))} Бан пользователей\n"
        f"{_mark(is_admin and getattr(member, 'can_restrict_members', False))} Ограничение пользователей\n"
        f"{_mark(is_admin and getattr(member, 'can_pin_messages', False))} Закрепление сообщений\n"
        f"{_mark(is_admin and (getattr(member, 'can_post_messages', None) is not False))} Публикация сообщений\n"
        f"{_mark(is_admin)} Получение событий"
    )


def create_group_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="groups")

    @router.my_chat_member()
    async def bot_membership(event: ChatMemberUpdated, bot: Bot) -> None:
        if event.chat.type not in {"group", "supergroup"}:
            return
        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        if old_status in {"left", "kicked"} and new_status in {"member", "administrator"}:
            async with session_factory() as session:
                async with session.begin():
                    await register_pending_group(session, event.chat)
            await bot.send_message(
                event.chat.id,
                "⏳ Группа ещё не подключена. Фактический владелец группы должен написать «подключить» в течение 1 минуты.",
            )

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.text.casefold() == "подключить")
    async def connect(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        try:
            async with session_factory() as session:
                async with session.begin():
                    await connect_group(session, bot, message.chat.id, message.from_user)
        except PermissionError as exc:
            if str(exc) == "only_chat_owner":
                await message.answer("Подключить группу может только её фактический владелец.")
                return
            if str(exc) == "bot_not_admin":
                await message.answer("Для подключения бот должен быть администратором группы.")
                return
            raise
        await message.answer("✅ Группа подключена.\n\n" + await rights_diagnostic(bot, message.chat.id))

    return router
