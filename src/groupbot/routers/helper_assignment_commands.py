from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, AuditLog, Group, GroupStatus, User
from groupbot.routers import admin_member_sync as admin_member_sync_module
from groupbot.routers import admin_rank_audit_actions as admin_rank_audit_actions_module
from groupbot.routers.admin_hierarchy import _ensure_standard_roles
from groupbot.routers.admin_rank_target_actions import _resolve_target
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.admin_rank_access import (
    CHAT_ADMIN,
    CHIEF,
    DEPUTY,
    VOICE_ADMIN,
    assignment_permission_error,
    can_open_rank_management,
    removal_permission_error,
)
from groupbot.services.audit import write_audit
from groupbot.services.helper_role_policy import HELPER_ROLE, prepare_helper_telegram_state
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_group


MENTOR_ADMIN_ROLES = {DEPUTY, CHIEF, CHAT_ADMIN, VOICE_ADMIN}


async def _helper_role(session: AsyncSession, chat_id: int) -> AdminRole | None:
    role = (
        await session.execute(
            select(AdminRole).where(
                AdminRole.chat_id == chat_id,
                AdminRole.name == HELPER_ROLE,
                AdminRole.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if role is not None:
        return role
    await _ensure_standard_roles(session, chat_id)
    return (
        await session.execute(
            select(AdminRole).where(
                AdminRole.chat_id == chat_id,
                AdminRole.name == HELPER_ROLE,
                AdminRole.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _assign_helper(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw_target: str,
) -> None:
    if message.from_user is None:
        return
    chat_id = message.chat.id
    actor_id = message.from_user.id

    async with session_factory() as session:
        async with session.begin():
            if not await can_open_rank_management(session, chat_id=chat_id, actor_id=actor_id):
                await message.reply("Ваш ранг не позволяет назначать Помощников или группа сейчас недоступна.")
                return

            user, telegram_member, error = await _resolve_target(
                bot,
                session,
                chat_id=chat_id,
                raw_target=raw_target,
            )
            if error or user is None or telegram_member is None:
                await message.reply(error or "Не удалось определить пользователя.")
                return
            if user.telegram_user_id == actor_id:
                await message.reply("Нельзя назначить самого себя Помощником.")
                return

            role = await _helper_role(session, chat_id)
            if role is None:
                await message.reply("Ранг «Помощник» сейчас недоступен.")
                return

            existing = (
                await session.execute(
                    select(AdminAssignment)
                    .where(
                        AdminAssignment.chat_id == chat_id,
                        AdminAssignment.user_id == user.telegram_user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            old_role = None
            if existing is not None and existing.role_id is not None:
                old_role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == existing.role_id))
                ).scalar_one_or_none()

            permission_error = await assignment_permission_error(
                session,
                chat_id=chat_id,
                actor_id=actor_id,
                target_id=user.telegram_user_id,
                new_role=role,
                existing=existing,
                old_role=old_role,
            )
            if permission_error:
                await message.reply(permission_error)
                return

            error = await prepare_helper_telegram_state(
                bot,
                session,
                chat_id=chat_id,
                target_id=user.telegram_user_id,
                role=role,
                telegram_member=telegram_member,
            )
            if error:
                await message.reply(error)
                return

            error = await admin_member_sync_module._assign_role(
                session,
                chat_id=chat_id,
                target_id=user.telegram_user_id,
                role=role,
                actor_id=actor_id,
            )
            if error:
                await message.reply(error)
                return

    helper_text = clickable_user_display(user)
    mentor_text = clickable_identity(
        telegram_user_id=message.from_user.id,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=None,
    )
    await message.reply(
        f"🔹 {helper_text} назначен «Помощником» — наставник {mentor_text}.",
        parse_mode="HTML",
    )


async def _remove_helper(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw_target: str,
) -> None:
    if message.from_user is None:
        return
    chat_id = message.chat.id
    actor_id = message.from_user.id

    async with session_factory() as session:
        async with session.begin():
            if not await can_open_rank_management(session, chat_id=chat_id, actor_id=actor_id):
                await message.reply("Ваш ранг не позволяет снимать Помощников или группа сейчас недоступна.")
                return

            user, _telegram_member, error = await _resolve_target(
                bot,
                session,
                chat_id=chat_id,
                raw_target=raw_target,
            )
            if error or user is None:
                await message.reply(error or "Не удалось определить пользователя.")
                return

            assignment = (
                await session.execute(
                    select(AdminAssignment)
                    .where(
                        AdminAssignment.chat_id == chat_id,
                        AdminAssignment.user_id == user.telegram_user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if assignment is None or assignment.role_id is None:
                await message.reply("У этого пользователя нет ранга «Помощник».")
                return

            role = (
                await session.execute(
                    select(AdminRole).where(
                        AdminRole.id == assignment.role_id,
                        AdminRole.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
            if role is None or role.name != HELPER_ROLE:
                await message.reply("У этого пользователя нет ранга «Помощник».")
                return

            permission_error = await removal_permission_error(
                session,
                chat_id=chat_id,
                actor_id=actor_id,
                assignment=assignment,
                role=role,
            )
            if permission_error:
                await message.reply(permission_error)
                return

            _telegram_demoted, error = await admin_rank_audit_actions_module._remove_assignment(
                bot,
                session,
                chat_id=chat_id,
                assignment=assignment,
                role=role,
                actor_id=actor_id,
            )
            if error:
                await message.reply(error)
                return

    helper_text = clickable_user_display(user)
    actor_text = clickable_identity(
        telegram_user_id=message.from_user.id,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=None,
    )
    await message.reply(
        f"🔹 {helper_text} снят с роли «Помощник» — снял {actor_text}.",
        parse_mode="HTML",
    )


async def _mentor_is_active(
    session: AsyncSession,
    *,
    chat_id: int,
    mentor_id: int,
) -> bool:
    if await is_group_owner(session, chat_id, mentor_id):
        return True
    role_name = (
        await session.execute(
            select(AdminRole.name)
            .join(AdminAssignment, AdminAssignment.role_id == AdminRole.id)
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == mentor_id,
                AdminRole.is_active.is_(True),
                AdminRole.name.in_(MENTOR_ADMIN_ROLES),
            )
        )
    ).scalar_one_or_none()
    return role_name is not None


def _violation_message_url(message: Message) -> str | None:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"
    chat_id = str(message.chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.message_id}"
    return None


async def _report_violation(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if message.from_user is None or message.reply_to_message is None:
        return
    chat_id = message.chat.id
    target = message.reply_to_message.from_user
    if target is None:
        await message.reply("Не удалось определить автора сообщения.")
        return

    async with session_factory() as session:
        group = (
            await session.execute(select(Group).where(Group.chat_id == chat_id))
        ).scalar_one_or_none()
        if group is None or group.status != GroupStatus.active.value:
            await message.reply("⚠️ Группа сейчас отключена в Mimorus.")
            return
        if await active_subscription_for_group(session, chat_id) is None:
            await message.reply("⚠️ Функции Mimorus временно недоступны: у группы нет активного тарифа.")
            return

        assignment = (
            await session.execute(
                select(AdminAssignment)
                .join(AdminRole, AdminRole.id == AdminAssignment.role_id)
                .where(
                    AdminAssignment.chat_id == chat_id,
                    AdminAssignment.user_id == message.from_user.id,
                    AdminRole.name == HELPER_ROLE,
                    AdminRole.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            return

        mentor_id = assignment.assigned_by_user_id
        if mentor_id is None:
            audit_rows = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.chat_id == chat_id,
                        AuditLog.event_type == "group.admin_rank_assigned",
                        AuditLog.target_type == "user",
                        AuditLog.target_id == str(message.from_user.id),
                    )
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(20)
                )
            ).scalars().all()
            for audit_row in audit_rows:
                if (
                    (audit_row.payload or {}).get("role_name") == HELPER_ROLE
                    and audit_row.actor_user_id is not None
                ):
                    mentor_id = audit_row.actor_user_id
                    assignment.assigned_by_user_id = mentor_id
                    break

        if mentor_id is None:
            await message.reply(
                "⚠️ Не удалось определить вашего наставника. "
                "Попросите администратора снять и назначить вам ранг Помощника заново."
            )
            return

        if not await _mentor_is_active(session, chat_id=chat_id, mentor_id=mentor_id):
            await message.reply(
                "⚠️ Ваш наставник больше не является действующим администратором этой группы. "
                "Попросите администрацию снять и назначить вам Помощника заново."
            )
            return

        mentor = (
            await session.execute(select(User).where(User.telegram_user_id == mentor_id))
        ).scalar_one_or_none()
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
        mentor_text = (
            clickable_user_display(mentor)
            if mentor is not None
            else clickable_identity(
                telegram_user_id=mentor_id,
                first_name="Администратор",
                username=None,
            )
        )
        await write_audit(
            session,
            "group.helper_violation_reported",
            chat_id=chat_id,
            actor_user_id=message.from_user.id,
            target_type="user",
            target_id=str(target.id),
            payload={
                "assigned_admin_id": mentor_id,
                "message_id": message.reply_to_message.message_id,
            },
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
        f"{mentor_text}, проверьте отмеченное сообщение !"
    )
    if url is None:
        private_text += "\n\n🔗 Прямая ссылка на сообщение недоступна для этого типа группы."

    try:
        await message.bot.send_message(
            mentor_id,
            private_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
        try:
            await message.bot.forward_message(
                chat_id=mentor_id,
                from_chat_id=chat_id,
                message_id=message.reply_to_message.message_id,
            )
        except TelegramBadRequest:
            await message.bot.copy_message(
                chat_id=mentor_id,
                from_chat_id=chat_id,
                message_id=message.reply_to_message.message_id,
            )
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.reply(
            "⚠️ Не удалось отправить нарушение наставнику в личные сообщения. "
            "Ему нужно сначала открыть Mimorus в личке и нажать /start."
        )
        return

    await message.reply("Отправил данное нарушение вашему наставнику в личные сообщения.")


def create_helper_assignment_commands_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="helper_assignment_commands")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "назначить помощника",
    )
    async def assign_helper_by_reply(message: Message, bot: Bot) -> None:
        replied = message.reply_to_message
        if replied is None or replied.from_user is None:
            await message.reply("Не удалось определить пользователя из сообщения.")
            return
        await _assign_helper(
            message,
            bot,
            session_factory,
            raw_target=str(replied.from_user.id),
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*назначить\s+помощника\s+(@?[A-Za-z0-9_]+)\s*$"),
    )
    async def assign_helper_by_target(message: Message, bot: Bot) -> None:
        parts = (message.text or "").strip().split(maxsplit=2)
        if len(parts) != 3:
            return
        await _assign_helper(
            message,
            bot,
            session_factory,
            raw_target=parts[2],
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "снять помощника",
    )
    async def remove_helper_by_reply(message: Message, bot: Bot) -> None:
        replied = message.reply_to_message
        if replied is None or replied.from_user is None:
            await message.reply("Не удалось определить пользователя из сообщения.")
            return
        await _remove_helper(
            message,
            bot,
            session_factory,
            raw_target=str(replied.from_user.id),
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*снять\s+помощника\s+(@?[A-Za-z0-9_]+)\s*$"),
    )
    async def remove_helper_by_target(message: Message, bot: Bot) -> None:
        parts = (message.text or "").strip().split(maxsplit=2)
        if len(parts) != 3:
            return
        await _remove_helper(
            message,
            bot,
            session_factory,
            raw_target=parts[2],
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "нарушение",
    )
    async def helper_violation(message: Message) -> None:
        await _report_violation(message, session_factory)

    return router
