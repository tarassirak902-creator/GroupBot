from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole, GroupMember, MemberStatus, User
from groupbot.routers.admin_hierarchy import (
    RANK_META,
    SPECIAL_STATUSES,
    STANDARD_NAMES,
    _assignment_count,
    _ensure_standard_roles,
)
from groupbot.routers.group_control import _owner_access
from groupbot.routers.identity_privacy import _known_group_users
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.users import upsert_user


async def _sync_telegram_admins(callback: CallbackQuery, session: AsyncSession, chat_id: int) -> int:
    admins = await callback.bot.get_chat_administrators(chat_id)
    synced = 0
    for member in admins:
        user = member.user
        if user.is_bot:
            continue
        await upsert_user(session, user)
        await session.execute(
            insert(GroupMember)
            .values(
                chat_id=chat_id,
                user_id=user.id,
                status=MemberStatus.member.value,
                joined_at=func.now(),
                last_activity_at=func.now(),
            )
            .on_conflict_do_update(
                constraint="uq_group_member_chat_user",
                set_={
                    "status": MemberStatus.member.value,
                    "left_at": None,
                    "last_activity_at": func.now(),
                },
            )
        )
        synced += 1
    return synced


def _picker_keyboard(users, callback_prefix: str, back_data: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, user in enumerate(users, start=1):
        rows.append([
            InlineKeyboardButton(
                text=f"✅ Выбрать #{index}",
                callback_data=f"{callback_prefix}:{user.telegram_user_id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _picker_users_text(users) -> str:
    if not users:
        return ""
    return "\n".join(
        f"{index}. {clickable_user_display(user)}"
        for index, user in enumerate(users, start=1)
    )


def _assignment_limit(role: AdminRole) -> int | None:
    meta = RANK_META.get(role.name)
    return meta[1] if meta is not None else None


def _role_back_data(chat_id: int, role: AdminRole) -> str:
    if role.name in STANDARD_NAMES:
        return f"hier:role:{chat_id}:{role.id}"
    return f"gctl:role:{chat_id}:{role.id}"


async def _assign_role(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    actor_id: int,
) -> str | None:
    existing = (
        await session.execute(
            select(AdminAssignment)
            .where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == target_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    limit = _assignment_limit(role)
    current_count = await _assignment_count(session, role.id)
    if limit is not None and current_count >= limit and (existing is None or existing.role_id != role.id):
        return f"Лимит назначений для «{role.name}» уже достигнут: {limit}."

    if existing is None:
        session.add(AdminAssignment(chat_id=chat_id, user_id=target_id, role_id=role.id, is_reserve=False))
    else:
        existing.role_id = role.id
        # Резервный администратор — независимый статус. При смене ранга его не снимаем.

    await write_audit(
        session,
        "group.admin_rank_assigned",
        chat_id=chat_id,
        actor_user_id=actor_id,
        target_type="user",
        target_id=str(target_id),
        payload={"role_id": role.id, "role_name": role.name},
    )
    return None


def create_admin_member_sync_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="admin_member_sync")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.reply_to_message,
        F.text.casefold() == "назначить",
    )
    async def assign_by_reply(message: Message) -> None:
        if message.from_user is None or message.reply_to_message is None or message.reply_to_message.from_user is None:
            return
        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.answer("Боту нельзя назначить административный ранг Mimorus.")
            return

        chat_id = message.chat.id
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await message.answer("Назначать ранги может владелец группы с активным тарифом.")
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

        if not roles:
            await message.answer("В группе пока нет доступных административных рангов.")
            return

        standard = [role for role in roles if role.name in STANDARD_NAMES]
        custom = [role for role in roles if role.name not in STANDARD_NAMES]
        ordered = standard + custom
        rows = [
            [InlineKeyboardButton(
                text=("👑 " if role.name in STANDARD_NAMES else "➕ ") + role.name[:58],
                callback_data=f"adminreply:set:{chat_id}:{target.id}:{role.id}",
            )]
            for role in ordered
        ]
        target_text = clickable_identity(
            telegram_user_id=target.id,
            first_name=target.first_name,
            last_name=target.last_name,
            username=target.username,
        )
        await message.answer(
            "👑 <b>Назначение ранга</b>\n\n"
            f"Пользователь: {target_text}\n\n"
            "Выберите ранг:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

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
            member = await callback.bot.get_chat_member(chat_id, target_id)
            if member.status in {"left", "kicked"}:
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
                f"✅ Пользователю {clickable_user_display(user)} назначен ранг «<b>{escape(role.name)}</b>».",
                parse_mode="HTML",
            )
        await callback.answer("Назначено")

    @router.callback_query(F.data.startswith("hier:assign:"))
    async def rank_picker(callback: CallbackQuery) -> None:
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
                        AdminRole.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if role is None:
                await callback.answer("Ранг не найден.", show_alert=True)
                return

            limit = _assignment_limit(role)
            if limit is not None and await _assignment_count(session, role_id) >= limit:
                await callback.answer(
                    f"Для ранга «{role.name}» достигнут лимит назначений: {limit}.",
                    show_alert=True,
                )
                return

            try:
                synced = await _sync_telegram_admins(callback, session, chat_id)
                await session.commit()
            except Exception:
                await session.rollback()
                synced = 0

            users = await _known_group_users(session, chat_id)

        if callback.message is not None:
            text = (
                f"➕ <b>Назначить ранг «{escape(role.name)}»</b>\n\n"
                "Выберите участника из списка ниже.\n"
                "Имя и @username кликабельны отдельно, а разделитель | — обычный текст."
            )
            if synced:
                text += f"\n\nАдминистраторы Telegram синхронизированы: <b>{synced}</b>."
            if users:
                text += "\n\n" + _picker_users_text(users)
            else:
                text += (
                    "\n\nПока нет известных участников. Обычные участники появляются здесь "
                    "после любой активности в группе."
                )
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_picker_keyboard(
                    users,
                    f"priv:rank_pick:{chat_id}:{role_id}",
                    _role_back_data(chat_id, role),
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("priv:rank_pick:"))
    async def rank_pick(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
            target_id = int(parts[4])
        except (ValueError, IndexError):
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
                f"✅ Пользователю {clickable_user_display(user)} назначен ранг «<b>{escape(role.name)}</b>».",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Назначенные", callback_data=f"hier:assigned:{chat_id}:{role_id}")],
                    [InlineKeyboardButton(text="◀️ К рангу", callback_data=back_data)],
                ]),
            )
        await callback.answer("Назначено")

    @router.callback_query(F.data.startswith("hier:special_add:"))
    async def special_picker(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            status = parts[3]
        except (ValueError, IndexError):
            return
        if status not in SPECIAL_STATUSES:
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            try:
                synced = await _sync_telegram_admins(callback, session, chat_id)
                await session.commit()
            except Exception:
                await session.rollback()
                synced = 0
            users = await _known_group_users(session, chat_id)

        if callback.message is not None:
            text = (
                f"➕ <b>{SPECIAL_STATUSES[status]}</b>\n\n"
                "Выберите участника из списка ниже.\n"
                "Имя и @username кликабельны отдельно, а разделитель | — обычный текст."
            )
            if synced:
                text += f"\n\nАдминистраторы Telegram синхронизированы: <b>{synced}</b>."
            if users:
                text += "\n\n" + _picker_users_text(users)
            else:
                text += "\n\nПока нет известных участников."
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_picker_keyboard(
                    users,
                    f"priv:special_pick:{chat_id}:{status}",
                    f"hier:special_list:{chat_id}:{status}",
                ),
            )
        await callback.answer()

    return router
