from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, GroupMember, MemberStatus, User
from groupbot.routers.admin_hierarchy import STANDARD_NAMES, _ensure_standard_roles
from groupbot.routers.admin_member_sync import (
    _assign_role,
    _assignment_limit_error,
    _ensure_telegram_admin_for_role,
    _role_back_data,
)
from groupbot.routers.admin_rank_audit_actions import _remove_assignment
from groupbot.routers.admin_rank_target_actions import _resolve_target
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.admin_rank_access import (
    assignment_permission_error,
    can_open_rank_management,
    removal_permission_error,
)
from groupbot.services.admin_rank_events import assignment_event, removal_event
from groupbot.services.users import upsert_user


async def _old_role(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
) -> tuple[AdminAssignment | None, AdminRole | None]:
    assignment = (
        await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None or assignment.role_id is None:
        return assignment, None
    role = (
        await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
    ).scalar_one_or_none()
    return assignment, role


async def _allowed_roles(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
    target_id: int,
    roles: list[AdminRole],
) -> list[AdminRole]:
    existing, old_role = await _old_role(session, chat_id=chat_id, target_id=target_id)
    allowed: list[AdminRole] = []
    for role in roles:
        error = await assignment_permission_error(
            session,
            chat_id=chat_id,
            actor_id=actor_id,
            target_id=target_id,
            new_role=role,
            existing=existing,
            old_role=old_role,
        )
        if error is None:
            allowed.append(role)
    return allowed


def _roles_keyboard(chat_id: int, target_id: int, roles: list[AdminRole]) -> InlineKeyboardMarkup:
    standard = [role for role in roles if role.name in STANDARD_NAMES]
    custom = [role for role in roles if role.name not in STANDARD_NAMES]
    rows = [
        [InlineKeyboardButton(
            text=("👑 " if role.name in STANDARD_NAMES else "➕ ") + role.name[:58],
            callback_data=f"adminreply:set:{chat_id}:{target_id}:{role.id}",
        )]
        for role in standard + custom
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

            existing, old_role = await _old_role(session, chat_id=chat_id, target_id=target_id)
            access_error = await assignment_permission_error(
                session,
                chat_id=chat_id,
                actor_id=callback.from_user.id,
                target_id=target_id,
                new_role=role,
                existing=existing,
                old_role=old_role,
            )
            if access_error:
                await callback.answer(access_error, show_alert=True)
                return

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
            await session.flush()
            current = (
                await session.execute(
                    select(AdminAssignment).where(
                        AdminAssignment.chat_id == chat_id,
                        AdminAssignment.user_id == target_id,
                    )
                )
            ).scalar_one()
            current.assigned_by_user_id = callback.from_user.id

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
            access_error = await removal_permission_error(
                session,
                chat_id=chat_id,
                actor_id=callback.from_user.id,
                assignment=assignment,
                role=role,
            )
            if access_error:
                await callback.answer(access_error, show_alert=True)
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

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "назначить",
    )
    async def open_assign_reply(message: Message) -> None:
        if message.from_user is None or message.reply_to_message is None or message.reply_to_message.from_user is None:
            return
        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.answer("Боту нельзя назначить административный ранг Mimorus.")
            return
        chat_id = message.chat.id
        async with session_factory() as session:
            async with session.begin():
                if not await can_open_rank_management(session, chat_id=chat_id, actor_id=message.from_user.id):
                    await message.answer("Ваш ранг не позволяет управлять администрацией.")
                    return
                await upsert_user(session, target)
                await session.execute(
                    insert(GroupMember)
                    .values(
                        chat_id=chat_id,
                        user_id=target.id,
                        status=MemberStatus.member.value,
                        joined_at=func.now(),
                        last_activity_at=func.now(),
                    )
                    .on_conflict_do_update(
                        constraint="uq_group_member_chat_user",
                        set_={"status": MemberStatus.member.value, "left_at": None},
                    )
                )
                await _ensure_standard_roles(session, chat_id)
                roles = list((
                    await session.execute(
                        select(AdminRole)
                        .where(AdminRole.chat_id == chat_id, AdminRole.is_active.is_(True))
                        .order_by(AdminRole.id)
                    )
                ).scalars().all())
                roles = await _allowed_roles(
                    session,
                    chat_id=chat_id,
                    actor_id=message.from_user.id,
                    target_id=target.id,
                    roles=roles,
                )
        if not roles:
            await message.answer("Для этого пользователя у вашего ранга нет доступных действий назначения/изменения.")
            return
        target_text = clickable_identity(
            telegram_user_id=target.id,
            first_name=target.first_name,
            last_name=target.last_name,
            username=target.username,
        )
        await message.answer(
            "👑 <b>Назначение ранга</b>\n\n"
            f"Пользователь: {target_text}\n\n"
            "Выберите доступный ранг:",
            parse_mode="HTML",
            reply_markup=_roles_keyboard(chat_id, target.id, roles),
        )

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*назначить\s+(@?[A-Za-z0-9_]+)\s*$"),
    )
    async def open_assign_target(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) != 2:
            return
        chat_id = message.chat.id
        async with session_factory() as session:
            async with session.begin():
                if not await can_open_rank_management(session, chat_id=chat_id, actor_id=message.from_user.id):
                    await message.answer("Ваш ранг не позволяет управлять администрацией.")
                    return
                user, _member, error = await _resolve_target(bot, session, chat_id=chat_id, raw_target=parts[1])
                if error or user is None:
                    await message.answer(error or "Пользователь не найден.")
                    return
                await _ensure_standard_roles(session, chat_id)
                roles = list((
                    await session.execute(
                        select(AdminRole)
                        .where(AdminRole.chat_id == chat_id, AdminRole.is_active.is_(True))
                        .order_by(AdminRole.id)
                    )
                ).scalars().all())
                roles = await _allowed_roles(
                    session,
                    chat_id=chat_id,
                    actor_id=message.from_user.id,
                    target_id=user.telegram_user_id,
                    roles=roles,
                )
        if not roles:
            await message.answer("Для этого пользователя у вашего ранга нет доступных действий назначения/изменения.")
            return
        await message.answer(
            "👑 <b>Назначение ранга</b>\n\n"
            f"Пользователь: {clickable_user_display(user)}\n\n"
            "Выберите доступный ранг:",
            parse_mode="HTML",
            reply_markup=_roles_keyboard(chat_id, user.telegram_user_id, roles),
        )

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
                if role is None:
                    await message.answer("Текущий административный ранг не найден.")
                    return
                access_error = await removal_permission_error(
                    session,
                    chat_id=chat_id,
                    actor_id=message.from_user.id,
                    assignment=assignment,
                    role=role,
                )
                if access_error:
                    await message.answer(f"❌ {access_error}")
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
        user = User(
            telegram_user_id=target.id,
            username=target.username,
            first_name=target.first_name,
            last_name=target.last_name,
            is_bot=target.is_bot,
        )
        await message.answer(removal_event(user, target.id, message.from_user), parse_mode="HTML")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*(?:снять|разжаловать)\s+(@?[A-Za-z0-9_]+)\s*$"),
    )
    async def open_remove_target(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) != 2:
            return
        chat_id = message.chat.id
        async with session_factory() as session:
            async with session.begin():
                user, _member, error = await _resolve_target(bot, session, chat_id=chat_id, raw_target=parts[1])
                if error or user is None:
                    await message.answer(error or "Пользователь не найден.")
                    return
                assignment = (
                    await session.execute(
                        select(AdminAssignment).where(
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.user_id == user.telegram_user_id,
                        )
                    )
                ).scalar_one_or_none()
                if assignment is None or assignment.role_id is None:
                    await message.answer("У этого пользователя нет назначенного административного ранга Mimorus.")
                    return
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
                ).scalar_one_or_none()
                if role is None:
                    await message.answer("Текущий административный ранг не найден.")
                    return
                access_error = await removal_permission_error(
                    session,
                    chat_id=chat_id,
                    actor_id=message.from_user.id,
                    assignment=assignment,
                    role=role,
                )
                if access_error:
                    await message.answer(f"❌ {access_error}")
                    return
        await message.answer(
            "⚠️ <b>Снять административный ранг?</b>\n\n"
            f"Пользователь: {clickable_user_display(user)}\n"
            f"Ранг: <b>{escape(role.name)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Снять ранг",
                    callback_data=f"admintext:remove:{chat_id}:{user.telegram_user_id}",
                )],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="noop")],
            ]),
        )

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
