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
    _ensure_telegram_admin_for_role,
    _role_back_data,
)
from groupbot.routers.admin_rank_audit_actions import _remove_assignment
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.admin_rank_events import assignment_event, removal_event


async def _old_role(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
) -> AdminRole | None:
    assignment = (
        await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None or assignment.role_id is None:
        return None
    return (
        await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
    ).scalar_one_or_none()


async def _assign_from_callback(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
    target_id: int,
    role_id: int,
    require_known_member: bool,
    notify_group_from_private: bool,
    with_private_navigation: bool,
) -> None:
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
            if require_known_member:
                member = (
                    await session.execute(
                        select(GroupMember).where(
                            GroupMember.chat_id == chat_id,
                            GroupMember.user_id == target_id,
                            GroupMember.status == MemberStatus.member.value,
                        )
                    )
                ).scalar_one_or_none()
            else:
                member = True
            if role is None or user is None or member is None:
                await callback.answer("Пользователь или ранг больше недоступен.", show_alert=True)
                return

            old_role = await _old_role(session, chat_id=chat_id, target_id=target_id)
            error = await _assignment_limit_error(
                session, chat_id=chat_id, target_id=target_id, role=role
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

    event_text = assignment_event(user, role, callback.from_user, old_role)
    if notify_group_from_private:
        try:
            await callback.bot.send_message(chat_id, event_text, parse_mode="HTML")
        except Exception:
            pass
        if callback.message is not None:
            back_data = _role_back_data(chat_id, role)
            markup = None
            if with_private_navigation:
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Назначенные", callback_data=f"hier:assigned:{chat_id}:{role_id}")],
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=back_data)],
                ])
            await callback.message.edit_text(
                "✅ <b>Ранг сохранён</b>\n\n"
                f"{clickable_user_display(user)} → <b>{escape(role.name)}</b>\n\n"
                "Уведомление отправлено в группу.",
                parse_mode="HTML",
                reply_markup=markup,
            )
    elif callback.message is not None:
        await callback.message.edit_text(event_text, parse_mode="HTML")
    await callback.answer("Сохранено")


async def _remove_from_callback(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
    target_id: int | None = None,
    assignment_id: int | None = None,
    role_id: int | None = None,
    notify_group_from_private: bool,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            query = select(AdminAssignment).where(AdminAssignment.chat_id == chat_id)
            if assignment_id is not None:
                query = query.where(AdminAssignment.id == assignment_id)
            if target_id is not None:
                query = query.where(AdminAssignment.user_id == target_id)
            if role_id is not None:
                query = query.where(AdminAssignment.role_id == role_id)
            assignment = (
                await session.execute(query.with_for_update())
            ).scalar_one_or_none()
            if assignment is None or assignment.role_id is None:
                await callback.answer("Ранг уже снят.", show_alert=True)
                return
            target_id = assignment.user_id
            actual_role_id = assignment.role_id
            role = (
                await session.execute(select(AdminRole).where(AdminRole.id == actual_role_id))
            ).scalar_one_or_none()
            user = (
                await session.execute(select(User).where(User.telegram_user_id == target_id))
            ).scalar_one_or_none()
            if role is None:
                await callback.answer("Ранг не найден.", show_alert=True)
                return
            _telegram_demoted, error = await _remove_assignment(
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

    event_text = removal_event(user, target_id, callback.from_user)
    if notify_group_from_private:
        try:
            await callback.bot.send_message(chat_id, event_text, parse_mode="HTML")
        except Exception:
            pass
        if callback.message is not None:
            target_text = clickable_user_display(user) if user is not None else str(target_id)
            await callback.message.edit_text(
                "✅ <b>Ранг снят</b>\n\n"
                f"👤 Пользователь: {target_text}\n\n"
                "Уведомление отправлено в группу.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=f"hier:role:{chat_id}:{actual_role_id}")]
                ]),
            )
    elif callback.message is not None:
        await callback.message.edit_text(event_text, parse_mode="HTML")
    await callback.answer("Ранг снят")


def create_admin_rank_compact_actions_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admin_rank_compact_actions")

    @router.callback_query(F.data.startswith("adminreply:set:"))
    async def assign_reply(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id, target_id, role_id = int(parts[2]), int(parts[3]), int(parts[4])
        except (ValueError, IndexError):
            return
        await _assign_from_callback(
            callback,
            session_factory,
            chat_id=chat_id,
            target_id=target_id,
            role_id=role_id,
            require_known_member=False,
            notify_group_from_private=False,
            with_private_navigation=False,
        )

    @router.callback_query(F.data.startswith("admintext:set:"))
    async def assign_text(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id, target_id, role_id = int(parts[2]), int(parts[3]), int(parts[4])
        except (ValueError, IndexError):
            return
        await _assign_from_callback(
            callback,
            session_factory,
            chat_id=chat_id,
            target_id=target_id,
            role_id=role_id,
            require_known_member=False,
            notify_group_from_private=False,
            with_private_navigation=False,
        )

    @router.callback_query(F.data.startswith("priv:rank_pick:"))
    async def assign_private_or_group(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id, role_id, target_id = int(parts[2]), int(parts[3]), int(parts[4])
        except (ValueError, IndexError):
            return
        is_private = callback.message is not None and callback.message.chat.type == "private"
        await _assign_from_callback(
            callback,
            session_factory,
            chat_id=chat_id,
            target_id=target_id,
            role_id=role_id,
            require_known_member=True,
            notify_group_from_private=is_private,
            with_private_navigation=is_private,
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold().in_({"снять", "разжаловать"}),
    )
    async def remove_reply(message: Message) -> None:
        if message.from_user is None or message.reply_to_message is None or message.reply_to_message.from_user is None:
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
                        select(AdminAssignment)
                        .where(
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.user_id == target.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if assignment is None or assignment.role_id is None:
                    await message.answer("У этого пользователя нет назначенного административного ранга Mimorus.")
                    return
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
                ).scalar_one_or_none()
                user = (
                    await session.execute(select(User).where(User.telegram_user_id == target.id))
                ).scalar_one_or_none()
                if role is None:
                    await message.answer("Текущий административный ранг не найден.")
                    return
                _telegram_demoted, error = await _remove_assignment(
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
        await message.answer(removal_event(user, target.id, message.from_user), parse_mode="HTML")

    @router.callback_query(F.data.startswith("admintext:remove:"))
    async def remove_text(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id, target_id = int(parts[2]), int(parts[3])
        except (ValueError, IndexError):
            return
        await _remove_from_callback(
            callback,
            session_factory,
            chat_id=chat_id,
            target_id=target_id,
            notify_group_from_private=False,
        )

    @router.callback_query(F.data.startswith("hier:remove:"))
    async def remove_private_or_group(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id, assignment_id, role_id = int(parts[2]), int(parts[3]), int(parts[4])
        except (ValueError, IndexError):
            return
        is_private = callback.message is not None and callback.message.chat.type == "private"
        await _remove_from_callback(
            callback,
            session_factory,
            chat_id=chat_id,
            assignment_id=assignment_id,
            role_id=role_id,
            notify_group_from_private=is_private,
        )

    return router
