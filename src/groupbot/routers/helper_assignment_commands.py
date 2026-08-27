from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole
from groupbot.routers import admin_member_sync as admin_member_sync_module
from groupbot.routers.admin_hierarchy import _ensure_standard_roles
from groupbot.routers.admin_rank_target_actions import _resolve_target
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.admin_rank_access import assignment_permission_error
from groupbot.services.helper_role_policy import HELPER_ROLE, prepare_helper_telegram_state


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

    return router
