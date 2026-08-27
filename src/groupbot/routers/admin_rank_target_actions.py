from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, GroupMember, MemberStatus, User
from groupbot.routers.admin_hierarchy import STANDARD_NAMES
from groupbot.routers.admin_member_sync import (
    _assign_role,
    _assignment_limit_error,
    _ensure_telegram_admin_for_role,
)
from groupbot.routers.admin_rank_audit_actions import _remove_assignment, _telegram_user_link
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.users import upsert_user


async def _resolve_target(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    raw_target: str,
) -> tuple[User | None, object | None, str | None]:
    token = raw_target.strip()
    if not token:
        return None, None, "Укажите @username или Telegram ID пользователя."

    if token.startswith("@"):
        username = token[1:].strip()
        if not username:
            return None, None, "После @ должен быть username пользователя."
        users = list((
            await session.execute(
                select(User)
                .join(GroupMember, GroupMember.user_id == User.telegram_user_id)
                .where(
                    GroupMember.chat_id == chat_id,
                    GroupMember.status == MemberStatus.member.value,
                    func.lower(User.username) == username.casefold(),
                )
                .limit(2)
            )
        ).scalars().all())
        if not users:
            return None, None, (
                "Пользователь с таким @username пока не найден среди известных участников этой группы. "
                "Попросите его написать сообщение в группе или используйте Telegram ID."
            )
        if len(users) > 1:
            return None, None, "Найдено несколько старых записей с этим username. Используйте Telegram ID."
        target_id = users[0].telegram_user_id
    else:
        try:
            target_id = int(token)
        except ValueError:
            return None, None, "Укажите пользователя в формате @username или числового Telegram ID."

    try:
        member = await bot.get_chat_member(chat_id, target_id)
    except Exception:
        return None, None, "Не удалось найти этого пользователя в группе. Проверьте username или ID."
    if member.status in {"left", "kicked"}:
        return None, None, "Этот пользователь сейчас не состоит в группе."
    if member.user.is_bot:
        return None, None, "Боту нельзя назначать административный ранг Mimorus."

    await upsert_user(session, member.user)
    await session.execute(
        insert(GroupMember)
        .values(
            chat_id=chat_id,
            user_id=target_id,
            status=MemberStatus.member.value,
            joined_at=func.now(),
            last_activity_at=func.now(),
        )
        .on_conflict_do_update(
            constraint="uq_group_member_chat_user",
            set_={"status": MemberStatus.member.value, "left_at": None},
        )
    )
    user = (
        await session.execute(select(User).where(User.telegram_user_id == target_id))
    ).scalar_one()
    return user, member, None


def _roles_keyboard(chat_id: int, target_id: int, roles: list[AdminRole]) -> InlineKeyboardMarkup:
    standard = [role for role in roles if role.name in STANDARD_NAMES]
    custom = [role for role in roles if role.name not in STANDARD_NAMES]
    ordered = standard + custom
    rows = [
        [InlineKeyboardButton(
            text=("👑 " if role.name in STANDARD_NAMES else "➕ ") + role.name[:58],
            callback_data=f"admintext:set:{chat_id}:{target_id}:{role.id}",
        )]
        for role in ordered
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_admin_rank_target_actions_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admin_rank_target_actions")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*назначить\s+(@?[A-Za-z0-9_]+)\s*$"),
    )
    async def assign_by_target(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) != 2:
            return
        chat_id = message.chat.id
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await message.answer("Назначать ранги может владелец группы с активным тарифом.")
                    return
                user, _member, error = await _resolve_target(
                    bot, session, chat_id=chat_id, raw_target=parts[1]
                )
                if error or user is None:
                    await message.answer(error or "Пользователь не найден.")
                    return
                roles = list((
                    await session.execute(
                        select(AdminRole)
                        .where(AdminRole.chat_id == chat_id, AdminRole.is_active.is_(True))
                        .order_by(AdminRole.id)
                    )
                ).scalars().all())
        if not roles:
            await message.answer("В группе пока нет доступных административных рангов.")
            return
        await message.answer(
            "👑 <b>Назначение ранга</b>\n\n"
            f"👤 Пользователь: {clickable_user_display(user)}\n"
            f"👮 Назначает: {_telegram_user_link(message.from_user)}\n\n"
            "Выберите ранг:",
            parse_mode="HTML",
            reply_markup=_roles_keyboard(chat_id, user.telegram_user_id, roles),
        )

    @router.callback_query(F.data.startswith("admintext:set:"))
    async def assign_selected_role(callback: CallbackQuery) -> None:
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

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*(?:снять|разжаловать)\s+(@?[A-Za-z0-9_]+)\s*$"),
    )
    async def remove_by_target(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) != 2:
            return
        chat_id = message.chat.id
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await message.answer("Снимать административные ранги может владелец группы с активным тарифом.")
                    return
                user, _member, error = await _resolve_target(
                    bot, session, chat_id=chat_id, raw_target=parts[1]
                )
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
        await message.answer(
            "⚠️ <b>Снять административный ранг?</b>\n\n"
            f"👤 Пользователь: {clickable_user_display(user)}\n"
            f"👑 Ранг: <b>{escape(role.name)}</b>\n"
            f"👮 Снимает: {_telegram_user_link(message.from_user)}",
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
    async def remove_confirmed(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            target_id = int(parts[3])
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
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.user_id == target_id,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if assignment is None or assignment.role_id is None:
                    await callback.answer("Ранг уже снят.", show_alert=True)
                    return
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == assignment.role_id))
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
        note = (
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
                f"{note}",
                parse_mode="HTML",
            )
        await callback.answer("Ранг снят")

    return router
