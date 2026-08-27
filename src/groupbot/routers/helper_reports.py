from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, AuditLog, User
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit

HELPER_ROLE = "Помощник"


def _message_url(message: Message) -> str | None:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"
    chat_id = str(message.chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.message_id}"
    return None


async def _mentor_id(session: AsyncSession, assignment: AdminAssignment) -> int | None:
    if assignment.assigned_by_user_id is not None:
        return assignment.assigned_by_user_id

    # Backward-compatible recovery for assignments created before assigned_by_user_id
    # was populated reliably: use the latest rank-assignment audit event.
    rows = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.chat_id == assignment.chat_id,
                AuditLog.event_type == "group.admin_rank_assigned",
                AuditLog.target_type == "user",
                AuditLog.target_id == str(assignment.user_id),
            )
            .order_by(AuditLog.id.desc())
            .limit(20)
        )
    ).scalars().all()
    for row in rows:
        payload = row.payload or {}
        if payload.get("role_name") == HELPER_ROLE and row.actor_user_id is not None:
            return row.actor_user_id
    return None


def _identity(user) -> str:
    return clickable_identity(
        telegram_user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=None,
    )


def create_helper_reports_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="helper_reports")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "нарушение",
    )
    async def report_violation(message: Message) -> None:
        if message.from_user is None or message.reply_to_message is None:
            return
        offender = message.reply_to_message.from_user
        if offender is None:
            await message.reply("Не удалось определить автора отмеченного сообщения.")
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

            mentor_id = await _mentor_id(session, assignment)
            if mentor_id is None:
                await message.reply(
                    "⚠️ Не удалось определить вашего наставника. "
                    "Попросите администратора снять и назначить вам ранг Помощника заново."
                )
                return

            mentor = (
                await session.execute(select(User).where(User.telegram_user_id == mentor_id))
            ).scalar_one_or_none()

            await write_audit(
                session,
                "group.helper_violation_reported",
                chat_id=message.chat.id,
                actor_user_id=message.from_user.id,
                target_type="user",
                target_id=str(offender.id),
                payload={
                    "mentor_user_id": mentor_id,
                    "message_id": message.reply_to_message.message_id,
                },
            )
            await session.commit()

        helper_text = _identity(message.from_user)
        offender_text = _identity(offender)
        mentor_text = (
            clickable_user_display(mentor)
            if mentor is not None
            else clickable_identity(
                telegram_user_id=mentor_id,
                first_name="Администратор",
                username=None,
            )
        )
        url = _message_url(message.reply_to_message)
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
            f"Нарушитель: {offender_text}\n"
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
                    from_chat_id=message.chat.id,
                    message_id=message.reply_to_message.message_id,
                )
            except TelegramBadRequest:
                # Protected content may not be forwardable; copy still gives the mentor
                # the actual offending content immediately after the report card.
                await message.bot.copy_message(
                    chat_id=mentor_id,
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

    return router
