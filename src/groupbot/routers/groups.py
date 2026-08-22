from aiogram import Bot, F, Router
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupStatus
from groupbot.services.audit import write_audit
from groupbot.services.diagnostics import rights_diagnostic
from groupbot.services.groups import connect_group, register_pending_group


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
                    await write_audit(
                        session,
                        "group.bot_added",
                        chat_id=event.chat.id,
                        actor_user_id=event.from_user.id if event.from_user else None,
                        target_type="group",
                        target_id=str(event.chat.id),
                    )
            await bot.send_message(
                event.chat.id,
                "⏳ Группа ещё не подключена. Фактический владелец группы должен написать «подключить» в течение 1 минуты.",
            )
            return

        if old_status in {"member", "administrator"} and new_status in {"left", "kicked"}:
            async with session_factory() as session:
                async with session.begin():
                    group = (await session.execute(
                        select(Group).where(Group.chat_id == event.chat.id).with_for_update()
                    )).scalar_one_or_none()
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
        suffix = "\n\n✅ Критические права доступны." if critical_ok else "\n\n⚠️ Не хватает критических прав: часть функций будет недоступна."
        await message.answer("✅ Группа подключена.\n\n" + diagnostic + suffix)

    return router
