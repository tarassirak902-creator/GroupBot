from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, GroupMember, MemberStatus, User
from groupbot.routers.admin_member_sync import (
    _assign_role,
    _assignment_limit_error,
    _ensure_telegram_admin_for_role,
    _role_back_data,
)
from groupbot.routers.admin_rank_audit_actions import _remove_assignment, _telegram_user_link
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_user_display


def _assigned_notification(user: User, role: AdminRole, actor) -> str:
    return (
        "👑 <b>Изменение администрации</b>\n\n"
        f"✅ {clickable_user_display(user)} назначен(а) на ранг "
        f"«<b>{escape(role.name)}</b>».\n"
        f"👮 Назначил: {_telegram_user_link(actor)}"
    )


def _removed_notification(target_text: str, role: AdminRole, actor, telegram_demoted: bool) -> str:
    telegram_note = (
        "Права администратора Telegram также сняты."
        if telegram_demoted
        else "Telegram-статус, назначенный вручную вне Mimorus, не изменён."
    )
    return (
        "👑 <b>Изменение администрации</b>\n\n"
        f"❌ С {target_text} снят ранг «<b>{escape(role.name)}</b>».\n"
        f"👮 Снял: {_telegram_user_link(actor)}\n\n"
        f"{telegram_note}"
    )


def create_admin_rank_group_notifications_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admin_rank_group_notifications")

    @router.callback_query(
        F.message.chat.type == "private",
        F.data.startswith("priv:rank_pick:"),
    )
    async def private_rank_pick(callback: CallbackQuery) -> None:
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

        try:
            await callback.bot.send_message(
                chat_id,
                _assigned_notification(user, role, callback.from_user),
                parse_mode="HTML",
            )
        except Exception:
            pass

        back_data = _role_back_data(chat_id, role)
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Ранг назначен</b>\n\n"
                f"👤 Пользователь: {clickable_user_display(user)}\n"
                f"👑 Ранг: <b>{escape(role.name)}</b>\n\n"
                "Уведомление отправлено в группу.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Назначенные", callback_data=f"hier:assigned:{chat_id}:{role_id}")],
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=back_data)],
                ]),
            )
        await callback.answer("Назначено")

    @router.callback_query(
        F.message.chat.type == "private",
        F.data.startswith("hier:remove:"),
    )
    async def private_remove_rank(callback: CallbackQuery) -> None:
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
                target_id = assignment.user_id
                role = (
                    await session.execute(
                        select(AdminRole).where(
                            AdminRole.id == role_id,
                            AdminRole.chat_id == chat_id,
                        )
                    )
                ).scalar_one_or_none()
                user = (
                    await session.execute(select(User).where(User.telegram_user_id == target_id))
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

        target_text = clickable_user_display(user) if user is not None else str(target_id)
        try:
            await callback.bot.send_message(
                chat_id,
                _removed_notification(target_text, role, callback.from_user, telegram_demoted),
                parse_mode="HTML",
            )
        except Exception:
            pass

        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Ранг снят</b>\n\n"
                f"👤 Пользователь: {target_text}\n"
                f"👑 Ранг: <b>{escape(role.name)}</b>\n\n"
                "Уведомление отправлено в группу.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=f"hier:role:{chat_id}:{role_id}")]
                ]),
            )
        await callback.answer("Ранг снят")

    return router
