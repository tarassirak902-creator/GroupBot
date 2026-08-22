from aiogram import Bot, F, Router
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User as TelegramUser,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupStatus
from groupbot.services.audit import write_audit
from groupbot.services.diagnostics import rights_diagnostic
from groupbot.services.groups import connect_group, register_pending_group
from groupbot.services.users import upsert_user


def _status_value(status) -> str:
    return getattr(status, "value", str(status))


async def _register_added_group(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    chat,
    actor_user: TelegramUser | None,
) -> bool:
    """Register a newly-added bot once and announce the connection deadline.

    Telegram may deliver both a my_chat_member update and a service message with
    new_chat_members. The database check keeps this idempotent and prevents two
    announcements / deadline resets for the same add event.
    """
    should_announce = False
    async with session_factory() as session:
        async with session.begin():
            if actor_user is not None:
                await upsert_user(session, actor_user)

            group = (
                await session.execute(
                    select(Group).where(Group.chat_id == chat.id).with_for_update()
                )
            ).scalar_one_or_none()

            if group is None or group.status == GroupStatus.left.value:
                await register_pending_group(session, chat)
                await write_audit(
                    session,
                    "group.bot_added",
                    chat_id=chat.id,
                    actor_user_id=actor_user.id if actor_user else None,
                    target_type="group",
                    target_id=str(chat.id),
                )
                should_announce = True

    if should_announce:
        await bot.send_message(
            chat.id,
            "⏳ <b>Группа ещё не подключена.</b>\n\n"
            "Фактический владелец группы должен отправить команду:\n"
            "<code>подключить</code>\n\n"
            "На подключение даётся 1 минута.",
            parse_mode="HTML",
        )
    return should_announce


def create_group_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="groups")

    @router.my_chat_member()
    async def bot_membership(event: ChatMemberUpdated, bot: Bot) -> None:
        if event.chat.type not in {"group", "supergroup"}:
            return

        old_status = _status_value(event.old_chat_member.status)
        new_status = _status_value(event.new_chat_member.status)

        if old_status in {"left", "kicked"} and new_status in {"member", "administrator"}:
            await _register_added_group(
                session_factory,
                bot,
                chat=event.chat,
                actor_user=event.from_user,
            )
            return

        if old_status in {"member", "administrator"} and new_status in {"left", "kicked"}:
            async with session_factory() as session:
                async with session.begin():
                    if event.from_user is not None:
                        await upsert_user(session, event.from_user)
                    group = (
                        await session.execute(
                            select(Group).where(Group.chat_id == event.chat.id).with_for_update()
                        )
                    ).scalar_one_or_none()
                    if group is not None:
                        group.status = GroupStatus.left.value
                        group.connect_deadline_at = None
                        group.disconnect_deadline_at = None
                        await write_audit(
                            session,
                            "group.bot_removed",
                            chat_id=event.chat.id,
                            actor_user_id=event.from_user.id if event.from_user else None,
                            target_type="group",
                            target_id=str(event.chat.id),
                        )

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
    async def bot_added_service_message(message: Message, bot: Bot) -> None:
        me = await bot.get_me()
        if not any(member.id == me.id for member in (message.new_chat_members or [])):
            return
        await _register_added_group(
            session_factory,
            bot,
            chat=message.chat,
            actor_user=message.from_user,
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

        diagnostic, critical_ok = await rights_diagnostic(bot, message.chat.id)
        suffix = (
            "\n\n✅ Критические права доступны."
            if critical_ok
            else "\n\n⚠️ Не хватает критических прав: часть функций будет недоступна."
        )

        me = await bot.get_me()
        keyboard = None
        if me.username:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🤖 Перейти в Mimorus",
                            url=f"https://t.me/{me.username}?start=group_connected",
                        )
                    ]
                ]
            )

        await message.answer(
            "✅ <b>Группа успешно подключена!</b>\n\n"
            "Для настройки Mimorus перейдите в личные сообщения с ботом "
            "или воспользуйтесь командой /help.\n\n"
            + diagnostic
            + suffix,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    return router
