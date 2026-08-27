from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, AuditLog, Group, GroupSettings, GroupStatus, User
from groupbot.routers import admin_member_sync as admin_member_sync_module
from groupbot.routers import admin_rank_audit_actions as admin_rank_audit_actions_module
from groupbot.routers import admin_rank_compact_actions as admin_rank_compact_actions_module
from groupbot.routers import admin_rank_group_notifications as admin_rank_group_notifications_module
from groupbot.routers import group_control_role_actions as group_control_role_actions_module
from groupbot.routers import group_control_ux as group_control_ux_module
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.helper_role_policy import (
    HELPER_ROLE,
    cleanup_helper_managed_admins,
    prepare_helper_telegram_state,
    remember_assignment_actor,
)
from groupbot.services.subscriptions import active_subscription_for_group


LOCKED_TEXT = (
    "🔒 Функции Mimorus для этой группы пока недоступны.\n\n"
    "Владельцу группы нужно открыть Mimorus в личных сообщениях и активировать пробный тариф TEST."
)


# These modules are imported before group_commands in main.py. Patch their references
# once so every existing assignment/settings path uses the same Helper policy.
_original_ensure_telegram_admin_for_role = admin_member_sync_module._ensure_telegram_admin_for_role
_original_assign_role = admin_member_sync_module._assign_role
_original_sync_role = group_control_role_actions_module._sync_managed_telegram_admins_for_role
_original_sync_role_state = group_control_role_actions_module._sync_managed_telegram_admins_for_role_state


async def _ensure_telegram_admin_for_role_with_helper_policy(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    telegram_member,
) -> str | None:
    if role.name == HELPER_ROLE:
        return await prepare_helper_telegram_state(
            bot,
            session,
            chat_id=chat_id,
            target_id=target_id,
            role=role,
            telegram_member=telegram_member,
        )
    return await _original_ensure_telegram_admin_for_role(
        bot,
        session,
        chat_id=chat_id,
        target_id=target_id,
        role=role,
        telegram_member=telegram_member,
    )


async def _assign_role_with_actor_tracking(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    actor_id: int,
) -> str | None:
    error = await _original_assign_role(
        session,
        chat_id=chat_id,
        target_id=target_id,
        role=role,
        actor_id=actor_id,
    )
    if error is None:
        await session.flush()
        await remember_assignment_actor(
            session,
            chat_id=chat_id,
            target_id=target_id,
            actor_id=actor_id,
        )
    return error


async def _sync_role_with_helper_policy(
    callback,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
) -> str | None:
    role_name = (
        await session.execute(
            select(AdminRole.name).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    if role_name == HELPER_ROLE:
        return await cleanup_helper_managed_admins(
            callback.bot,
            session,
            chat_id=chat_id,
            role_id=role_id,
        )
    return await _original_sync_role(
        callback,
        session,
        chat_id=chat_id,
        role_id=role_id,
    )


async def _sync_role_state_with_helper_policy(
    callback,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
    enabled: bool,
) -> str | None:
    role_name = (
        await session.execute(
            select(AdminRole.name).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    if role_name == HELPER_ROLE:
        return await cleanup_helper_managed_admins(
            callback.bot,
            session,
            chat_id=chat_id,
            role_id=role_id,
        )
    return await _original_sync_role_state(
        callback,
        session,
        chat_id=chat_id,
        role_id=role_id,
        enabled=enabled,
    )


for _rank_module in (
    admin_member_sync_module,
    admin_rank_compact_actions_module,
    admin_rank_audit_actions_module,
    admin_rank_group_notifications_module,
):
    _rank_module._ensure_telegram_admin_for_role = _ensure_telegram_admin_for_role_with_helper_policy
    _rank_module._assign_role = _assign_role_with_actor_tracking

group_control_role_actions_module._sync_managed_telegram_admins_for_role = _sync_role_with_helper_policy
group_control_role_actions_module._sync_managed_telegram_admins_for_role_state = _sync_role_state_with_helper_policy
group_control_ux_module._sync_managed_telegram_admins_for_role = _sync_role_with_helper_policy
group_control_ux_module._sync_managed_telegram_admins_for_role_state = _sync_role_state_with_helper_policy


class HelperRoleCardFilter(Filter):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, callback: CallbackQuery) -> bool:
        data = callback.data or ""
        if not (data.startswith("hier:role:") or data.startswith("gctl:role:")):
            return False
        parts = data.split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return False
        async with self.session_factory() as session:
            role_name = (
                await session.execute(
                    select(AdminRole.name).where(
                        AdminRole.id == role_id,
                        AdminRole.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
        return role_name == HELPER_ROLE


def _violation_message_url(message: Message) -> str | None:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"
    chat_id = str(message.chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.message_id}"
    return None


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

    @router.callback_query(HelperRoleCardFilter(session_factory))
    async def helper_role_card(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            role = (
                await session.execute(
                    select(AdminRole).where(
                        AdminRole.id == role_id,
                        AdminRole.chat_id == chat_id,
                        AdminRole.name == HELPER_ROLE,
                    )
                )
            ).scalar_one_or_none()
            if role is None:
                await callback.answer("Ранг Помощник не найден.", show_alert=True)
                return
            count = (
                await session.execute(
                    select(func.count()).select_from(AdminAssignment).where(
                        AdminAssignment.chat_id == chat_id,
                        AdminAssignment.role_id == role_id,
                    )
                )
            ).scalar_one()

        if callback.message is not None:
            await callback.message.edit_text(
                "🔹 <b>Помощник</b>\n\n"
                "Помощник — не администратор Telegram и не модератор. Он помогает своему наставнику находить нарушения в группе.\n\n"
                f"Назначено Помощников: <b>{count}</b>\n\n"
                "Как работает:\n"
                "• Помощник закрепляется за конкретным администратором-наставником.\n"
                "• Ответом на нарушающее сообщение пишет <code>нарушение</code>.\n"
                "• Mimorus отправляет наставнику карточку нарушения в личные сообщения и пересылает само сообщение.\n"
                "• Предупреждения, муты, баны, удаление и другие модерационные команды Помощнику недоступны.\n\n"
                "Назначать Помощников могут Владелец, Зам. владельца, Глав. админ, Администратор чата и Администратор войса в пределах утверждённой иерархии.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Назначить Помощника", callback_data=f"hier:assign:{chat_id}:{role_id}")],
                    [InlineKeyboardButton(text="📋 Назначенные Помощники", callback_data=f"hier:assigned:{chat_id}:{role_id}")],
                    [InlineKeyboardButton(text="◀️ Ранги администрации", callback_data=f"gctl:roles:{chat_id}")],
                ]),
            )
        await callback.answer()

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
                audit_rows = (
                    await session.execute(
                        select(AuditLog)
                        .where(
                            AuditLog.chat_id == message.chat.id,
                            AuditLog.event_type == "group.admin_rank_assigned",
                            AuditLog.target_type == "user",
                            AuditLog.target_id == str(message.from_user.id),
                        )
                        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        .limit(20)
                    )
                ).scalars().all()
                for audit_row in audit_rows:
                    if (audit_row.payload or {}).get("role_name") == HELPER_ROLE and audit_row.actor_user_id is not None:
                        admin_id = audit_row.actor_user_id
                        assignment.assigned_by_user_id = admin_id
                        break

            if admin_id is None:
                await message.reply(
                    "⚠️ Не удалось определить вашего наставника. "
                    "Попросите администратора снять и назначить вам ранг Помощника заново."
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

        url = _violation_message_url(message.reply_to_message)
        markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="🔗 Перейти к сообщению в группе", url=url)
                ]]
            )
            if url is not None
            else None
        )
        private_text = (
            "🚨 <b>Помощник сообщил о нарушении !</b>\n\n"
            f"Помощник: {helper_text}\n"
            f"Нарушитель: {target_text}\n"
            "Причина: сообщение отмечено как нарушение.\n\n"
            f"{admin_text}, проверьте отмеченное сообщение !"
        )
        if url is None:
            private_text += "\n\n🔗 Прямая ссылка на сообщение недоступна для этого типа группы."

        try:
            await message.bot.send_message(
                admin_id,
                private_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
            try:
                await message.bot.forward_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.reply_to_message.message_id,
                )
            except TelegramBadRequest:
                await message.bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.reply_to_message.message_id,
                )
        except (TelegramForbiddenError, TelegramBadRequest):
            await message.reply(
                "⚠️ Не удалось отправить нарушение наставнику в личные сообщения. "
                "Ему нужно сначала открыть Mimorus в личке и нажать /start."
            )
            return

        await message.reply("Отправил данное нарушение вашему наставнику в личные сообщения.")

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
