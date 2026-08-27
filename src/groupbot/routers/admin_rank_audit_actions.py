from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, GroupMember, MemberStatus, User
from groupbot.routers.admin_member_sync import (
    _assign_role,
    _assignment_limit_error,
    _check_bot_promotion_rights,
    _ensure_telegram_admin_for_role,
    _role_back_data,
)
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.telegram_admin_models import TelegramAdminPromotion


DEMOTE_RIGHTS = {
    "can_manage_chat": False,
    "can_delete_messages": False,
    "can_manage_video_chats": False,
    "can_restrict_members": False,
    "can_promote_members": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_post_stories": False,
    "can_edit_stories": False,
    "can_delete_stories": False,
    "can_post_messages": False,
    "can_edit_messages": False,
    "can_pin_messages": False,
    "can_manage_topics": False,
}


def _telegram_user_link(user) -> str:
    return clickable_identity(
        telegram_user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
    )


async def _demote_if_managed(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
) -> tuple[bool, str | None]:
    promotion = (
        await session.execute(
            select(TelegramAdminPromotion)
            .where(
                TelegramAdminPromotion.chat_id == chat_id,
                TelegramAdminPromotion.user_id == target_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if promotion is None:
        return False, None

    try:
        member = await bot.get_chat_member(chat_id, target_id)
    except Exception:
        member = None

    if member is not None and member.status == "administrator":
        error = await _check_bot_promotion_rights(bot, chat_id, {})
        if error:
            return False, error
        try:
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                is_anonymous=False,
                **DEMOTE_RIGHTS,
            )
        except Exception:
            return False, (
                "Telegram не позволил снять права администратора. "
                "Проверьте право Mimorus назначать администраторов."
            )

    await session.delete(promotion)
    return True, None


async def _remove_assignment(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    assignment: AdminAssignment,
    role: AdminRole,
    actor_id: int,
) -> tuple[bool, str | None]:
    telegram_demoted, error = await _demote_if_managed(
        bot,
        session,
        chat_id=chat_id,
        target_id=assignment.user_id,
    )
    if error:
        return False, error

    target_id = assignment.user_id
    if assignment.is_reserve:
        assignment.role_id = None
    else:
        await session.delete(assignment)

    await write_audit(
        session,
        "group.admin_rank_removed",
        chat_id=chat_id,
        actor_user_id=actor_id,
        target_type="user",
        target_id=str(target_id),
        payload={"role_id": role.id, "role_name": role.name},
    )
    return telegram_demoted, None


def create_admin_rank_audit_actions_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admin_rank_audit_actions")

    @router.callback_query(F.data.startswith("adminreply:set:"))
    async def assign_reply_role(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            target_id = int(parts[3])
            role_id = int(parts[4])
        except (ValueError, IndexError):
            return

        try:
            telegram_member = await callback.bot.get_chat_member(chat_id, target_id)
            if telegram_member.status in {"left", "kicked"}:
                await callback.answer("Пользователь больше не состоит в группе.", show_alert=True)
                return
        except Exception:
            await callback.answer("Не удалось проверить участника группы.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                role = (
                    await session.execute(
                        select(AdminRole).where(
                            AdminRole.id == role_id,
                            AdminRole.chat_id == chat_id,
                            AdminRole.is_active.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                user = (
                    await session.execute(select(User).where(User.telegram_user_id == target_id))
                ).scalar_one_or_none()
                if role is None or user is None:
                    await callback.answer("Пользователь или ранг больше недоступен.", show_alert=True)
                    return

                error = await _assignment_limit_error(
                    session,
                    chat_id=chat_id,
                    target_id=target_id,
                    role=role,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return
                error = await _ensure_telegram_admin_for_role(
                    callback.bot,
                    session,
                    chat_id=chat_id,
                    target_id=target_id,
                    role=role,
                    telegram_member=telegram_member,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return
                error = await _assign_role(
                    session,
                    chat_id=chat_id,
                    target_id=target_id,
                    role=role,
                    actor_id=callback.from_user.id,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return

        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Административный ранг назначен</b>\n\n"
                f"👤 Пользователь: {clickable_user_display(user)}\n"
                f"👑 Ранг: <b>{escape(role.name)}</b>\n"
                f"👮 Назначил: {_telegram_user_link(callback.from_user)}\n\n"
                "Telegram-права синхронизированы там, где Mimorus управляет назначением.",
                parse_mode="HTML",
            )
        await callback.answer("Назначено")

    @router.callback_query(F.data.startswith("priv:rank_pick:"))
    async def rank_pick(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
            target_id = int(parts[4])
        except (ValueError, IndexError):
            return

        try:
            telegram_member = await callback.bot.get_chat_member(chat_id, target_id)
            if telegram_member.status in {"left", "kicked"}:
                await callback.answer("Пользователь больше не состоит в группе.", show_alert=True)
                return
        except Exception:
            await callback.answer("Не удалось проверить участника группы.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                role = (
                    await session.execute(
                        select(AdminRole).where(
                            AdminRole.id == role_id,
                            AdminRole.chat_id == chat_id,
                            AdminRole.is_active.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                user = (
                    await session.execute(select(User).where(User.telegram_user_id == target_id))
                ).scalar_one_or_none()
                member = (
                    await session.execute(
                        select(GroupMember).where(
                            GroupMember.chat_id == chat_id,
                            GroupMember.user_id == target_id,
                            GroupMember.status == MemberStatus.member.value,
                        )
                    )
                ).scalar_one_or_none()
                if role is None or user is None or member is None:
                    await callback.answer("Пользователь или ранг больше недоступен.", show_alert=True)
                    return

                error = await _assignment_limit_error(
                    session,
                    chat_id=chat_id,
                    target_id=target_id,
                    role=role,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return
                error = await _ensure_telegram_admin_for_role(
                    callback.bot,
                    session,
                    chat_id=chat_id,
                    target_id=target_id,
                    role=role,
                    telegram_member=telegram_member,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return
                error = await _assign_role(
                    session,
                    chat_id=chat_id,
                    target_id=target_id,
                    role=role,
                    actor_id=callback.from_user.id,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return

        back_data = _role_back_data(chat_id, role)
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Административный ранг назначен</b>\n\n"
                f"👤 Пользователь: {clickable_user_display(user)}\n"
                f"👑 Ранг: <b>{escape(role.name)}</b>\n"
                f"👮 Назначил: {_telegram_user_link(callback.from_user)}\n\n"
                "Telegram-права синхронизированы там, где Mimorus управляет назначением.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Назначенные", callback_data=f"hier:assigned:{chat_id}:{role_id}")],
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=back_data)],
                ]),
            )
        await callback.answer("Назначено")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold().in_({"снять", "разжаловать"}),
    )
    async def remove_by_reply(message: Message) -> None:
        if (
            message.from_user is None
            or message.reply_to_message is None
            or message.reply_to_message.from_user is None
        ):
            return
        target = message.reply_to_message.from_user
        chat_id = message.chat.id

        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await message.answer("Снимать административные ранги может владелец группы с активным тарифом.")
                    return

                assignment = (
                    await session.execute(
                        select(AdminAssignment).where(
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.user_id == target.id,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if assignment is None or assignment.role_id is None:
                    await message.answer("У этого пользователя нет назначенного административного ранга Mimorus.")
                    return
                role = (
                    await session.execute(
                        select(AdminRole).where(
                            AdminRole.id == assignment.role_id,
                            AdminRole.chat_id == chat_id,
                        )
                    )
                ).scalar_one_or_none()
                if role is None:
                    await message.answer("Текущий административный ранг не найден.")
                    return

                telegram_demoted, error = await _remove_assignment(
                    message.bot,
                    session,
                    chat_id=chat_id,
                    assignment=assignment,
                    role=role,
                    actor_id=message.from_user.id,
                )
                if error:
                    await message.answer(f"❌ {error}")
                    return

        telegram_note = (
            "Telegram-права администратора также сняты."
            if telegram_demoted
            else "Telegram-статус, назначенный вручную вне Mimorus, не изменён."
        )
        await message.answer(
            "✅ <b>Административный ранг снят</b>\n\n"
            f"👤 Пользователь: {_telegram_user_link(target)}\n"
            f"👑 Ранг: <b>{escape(role.name)}</b>\n"
            f"👮 Снял: {_telegram_user_link(message.from_user)}\n\n"
            f"{telegram_note}",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("hier:remove:"))
    async def remove_rank(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            assignment_id = int(parts[3])
            role_id = int(parts[4])
        except (ValueError, IndexError):
            return

        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                assignment = (
                    await session.execute(
                        select(AdminAssignment).where(
                            AdminAssignment.id == assignment_id,
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.role_id == role_id,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if assignment is None:
                    await callback.answer("Назначение уже снято.", show_alert=True)
                    return
                role = (
                    await session.execute(
                        select(AdminRole).where(
                            AdminRole.id == role_id,
                            AdminRole.chat_id == chat_id,
                        )
                    )
                ).scalar_one_or_none()
                user = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == assignment.user_id)
                    )
                ).scalar_one_or_none()
                if role is None:
                    await callback.answer("Ранг не найден.", show_alert=True)
                    return

                telegram_demoted, error = await _remove_assignment(
                    callback.bot,
                    session,
                    chat_id=chat_id,
                    assignment=assignment,
                    role=role,
                    actor_id=callback.from_user.id,
                )
                if error:
                    await callback.answer(error, show_alert=True)
                    return

        target_text = clickable_user_display(user) if user is not None else str(assignment.user_id)
        telegram_note = (
            "Telegram-права администратора также сняты."
            if telegram_demoted
            else "Telegram-статус, назначенный вручную вне Mimorus, не изменён."
        )
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Административный ранг снят</b>\n\n"
                f"👤 Пользователь: {target_text}\n"
                f"👑 Ранг: <b>{escape(role.name)}</b>\n"
                f"👮 Снял: {_telegram_user_link(callback.from_user)}\n\n"
                f"{telegram_note}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=f"hier:role:{chat_id}:{role_id}")]
                ]),
            )
        await callback.answer("Ранг снят")

    return router
