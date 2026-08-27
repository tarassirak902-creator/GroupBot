from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, AuditLog, Group, GroupSettings, GroupStatus, User
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.subscriptions import active_subscription_for_group


LOCKED_TEXT = (
    "🔒 Функции Mimorus для этой группы пока недоступны.\n\n"
    "Владельцу группы нужно открыть Mimorus в личных сообщениях и активировать пробный тариф TEST."
)
HELPER_ROLE = "Помощник"


def create_group_commands_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="group_commands")

    async def access_allowed(chat_id: int) -> tuple[bool, str | None]:
        async with session_factory() as session:
            group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
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

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "нарушение",
    )
    async def helper_violation(message: Message) -> None:
        if message.from_user is None or message.reply_to_message is None:
            return
        if not await require_access(message):
            return
        target = message.reply_to_message.from_user
        if target is None:
            await message.reply("Не удалось определить автора сообщения.")
            return

        async with session_factory() as session:
            assignment = (
                await session.execute(
                    select(AdminAssignment)
                    .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
                    .where(
                        AdminAssignment.chat_id == message.chat.id,
                        AdminAssignment.user_id == message.from_user.id,
                        AdminRole.name == HELPER_ROLE,
                        AdminRole.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if assignment is None:
                return

            admin_id = assignment.assigned_by_user_id
            if admin_id is None:
                # Compatibility for Helper assignments created before assigned_by_user_id
                # started being populated consistently: recover the actor from the latest
                # rank-assignment audit entry instead of guessing.
                admin_id = (
                    await session.execute(
                        select(AuditLog.actor_user_id)
                        .where(
                            AuditLog.chat_id == message.chat.id,
                            AuditLog.event_type == "group.admin_rank_assigned",
                            AuditLog.target_type == "user",
                            AuditLog.target_id == str(message.from_user.id),
                        )
                        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if admin_id is not None:
                    assignment.assigned_by_user_id = admin_id

            if admin_id is None:
                await message.reply(
                    "⚠️ Не удалось определить администратора, назначившего Помощника. "
                    "Снимите и назначьте Помощника заново."
                )
                return

            admin = (await session.execute(select(User).where(User.telegram_user_id == admin_id))).scalar_one_or_none()
            helper_text = clickable_identity(
                telegram_user_id=message.from_user.id,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
                username=None,
            )
            target_text = clickable_identity(
                telegram_user_id=target.id,
                first_name=target.first_name,
                last_name=target.last_name,
                username=None,
            )
            admin_text = clickable_user_display(admin) if admin is not None else clickable_identity(
                telegram_user_id=admin_id,
                first_name="Администратор",
                username=None,
            )
            await write_audit(
                session,
                "group.helper_violation_reported",
                chat_id=message.chat.id,
                actor_user_id=message.from_user.id,
                target_type="user",
                target_id=str(target.id),
                payload={"assigned_admin_id": admin_id, "message_id": message.reply_to_message.message_id},
            )
            await session.commit()

        await message.reply_to_message.reply(
            "🚨 <b>Помощник сообщил о нарушении</b>\n\n"
            f"Помощник: {helper_text}\n"
            f"Нарушитель: {target_text}\n"
            f"Ответственный администратор: {admin_text}\n\n"
            f"{admin_text}, проверьте отмеченное сообщение.",
            parse_mode="HTML",
        )

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
            "/help — помощь\n/guide — как пользоваться\n/commands — все команды\n/games — игры и развлечения\n"
            "/profile — мой профиль\n/stats — моя активность\n/rules — правила группы\n/support — помощь и поддержка"
        )

    @router.message(Command("games"), F.chat.type.in_({"group", "supergroup"}))
    async def games_command(message: Message) -> None:
        if not await require_access(message):
            return
        await message.answer(
            "🎮 Игры и развлечения\n\n🎮 Игры\n💕 Отношения\n🎭 RP-команды\n🎯 Задания\n🏆 Рейтинги\n👤 Игровой профиль\n\n"
            "Игровые механики будут подключаться следующими функциональными блоками."
        )

    @router.message(Command("profile"), F.chat.type.in_({"group", "supergroup"}))
    async def profile_command(message: Message) -> None:
        if not await require_access(message) or message.from_user is None:
            return
        identity = clickable_identity(
            telegram_user_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        await message.answer(
            "👤 <b>Профиль</b>\n\n" f"Пользователь: {identity}\n\n"
            "Расширенная карточка пользователя будет подключена в блоке статистики и профилей.",
            parse_mode="HTML",
        )

    @router.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
    async def stats_command(message: Message) -> None:
        if await require_access(message):
            await message.answer("📊 Моя активность\n\nСтатистика активности будет подключена отдельным блоком.")

    @router.message(Command("rules"), F.chat.type.in_({"group", "supergroup"}))
    async def rules_command(message: Message) -> None:
        if not await require_access(message):
            return
        async with session_factory() as session:
            rules = (await session.execute(select(GroupSettings.rules_text).where(GroupSettings.chat_id == message.chat.id))).scalar_one_or_none()
        await message.answer("📜 Правила группы\n\n" + (rules or "Правила группы пока не настроены владельцем."))

    @router.message(Command("support"), F.chat.type.in_({"group", "supergroup"}))
    async def support_command(message: Message) -> None:
        if await require_access(message):
            await message.answer(
                "🛠 Помощь и поддержка\n\n"
                "Управление обращениями находится в личных сообщениях с Mimorus в разделе «🛠 Поддержка»."
            )

    return router
