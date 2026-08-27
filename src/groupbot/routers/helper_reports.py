from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, User
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit

HELPER_ROLE = "Помощник"


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
            if assignment.assigned_by_user_id is None:
                await message.reply(
                    "⚠️ Для этого назначения Помощника не сохранён назначивший администратор. "
                    "Снимите и назначьте Помощника заново."
                )
                return
            admin = (
                await session.execute(
                    select(User).where(User.telegram_user_id == assignment.assigned_by_user_id)
                )
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
            admin_text = (
                clickable_user_display(admin)
                if admin is not None
                else clickable_identity(
                    telegram_user_id=assignment.assigned_by_user_id,
                    first_name="Администратор",
                    username=None,
                )
            )
            await write_audit(
                session,
                "group.helper_violation_reported",
                chat_id=message.chat.id,
                actor_user_id=message.from_user.id,
                target_type="user",
                target_id=str(target.id),
                payload={"assigned_admin_id": assignment.assigned_by_user_id, "message_id": message.reply_to_message.message_id},
            )
            await session.commit()

        await message.reply_to_message.reply(
            "🚨 <b>Сообщение отмечено как нарушение</b>\n\n"
            f"Помощник: {helper_text}\n"
            f"Нарушитель: {target_text}\n"
            f"Ответственный администратор: {admin_text}\n\n"
            f"{admin_text}, проверьте сообщение выше.",
            parse_mode="HTML",
        )

    return router
