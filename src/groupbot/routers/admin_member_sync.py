from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole, GroupMember, MemberStatus, User
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
from groupbot.telegram_admin_models import TelegramAdminPromotion


TELEGRAM_RESTRICT_PERMISSIONS = frozenset({"mute", "ban", "unmute", "unban"})


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


async def _assignment_limit_error(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
) -> str | None:
    existing = (
        await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.user_id == target_id,
            )
        )
    ).scalar_one_or_none()
    limit = _assignment_limit(role)
    current_count = await _assignment_count(session, role.id)
    if limit is not None and current_count >= limit and (existing is None or existing.role_id != role.id):
        return f"Лимит назначений для «{role.name}» уже достигнут: {limit}."
    return None


async def _telegram_rights_for_role(session: AsyncSession, role_id: int) -> dict[str, bool]:
    allowed = set((
        await session.execute(
            select(AdminPermission.permission).where(
                AdminPermission.role_id == role_id,
                AdminPermission.allowed.is_(True),
            )
        )
    ).scalars().all())
    return {
        "can_manage_chat": True,
        "can_delete_messages": "delete" in allowed,
        "can_manage_video_chats": False,
        "can_restrict_members": bool(allowed & TELEGRAM_RESTRICT_PERMISSIONS),
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_post_messages": False,
        "can_edit_messages": False,
        "can_pin_messages": "pin" in allowed,
        "can_manage_topics": False,
    }


async def _check_bot_promotion_rights(bot, chat_id: int, rights: dict[str, bool]) -> str | None:
    me = await bot.get_me()
    member = await bot.get_chat_member(chat_id, me.id)
    if member.status != "administrator":
        return "Mimorus не является администратором группы."
    if not bool(getattr(member, "can_promote_members", False)):
        return "У Mimorus нет права назначать администраторов."
    required = (
        ("can_delete_messages", "удаление сообщений"),
        ("can_restrict_members", "блокировка/ограничение участников"),
        ("can_pin_messages", "закрепление/открепление сообщений"),
    )
    for key, title in required:
        if rights.get(key) and not bool(getattr(member, key, False)):
            return f"Mimorus не может выдать право «{title}», потому что сам его не имеет."
    return None


async def _ensure_telegram_admin_for_role(
    bot,
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    role: AdminRole,
    telegram_member,
) -> str | None:
    if telegram_member.status == "creator":
        return "Владелец группы уже имеет максимальные права Telegram и отдельный ранг ему не назначается."

    promotion = (
        await session.execute(
            select(TelegramAdminPromotion).where(
                TelegramAdminPromotion.chat_id == chat_id,
                TelegramAdminPromotion.user_id == target_id,
            )
        )
    ).scalar_one_or_none()

    if telegram_member.status == "administrator" and promotion is None:
        return None

    rights = await _telegram_rights_for_role(session, role.id)
    error = await _check_bot_promotion_rights(bot, chat_id, rights)
    if error:
        return error

    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=target_id,
            is_anonymous=False,
            **rights,
        )
    except Exception:
        return "Telegram не позволил назначить права администратора. Проверьте права Mimorus и пользователя."

    if promotion is None:
        session.add(TelegramAdminPromotion(chat_id=chat_id, user_id=target_id))
    return None


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
        session.add(
            AdminAssignment(
                chat_id=chat_id,
                user_id=target_id,
                role_id=role.id,
                assigned_by_user_id=actor_id,
                is_reserve=False,
            )
        )
    else:
        existing.role_id = role.id
        existing.assigned_by_user_id = actor_id
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


async def _remove_role_and_managed_telegram_admin(
    callback: CallbackQuery,
    session: AsyncSession,
    *,
    chat_id: int,
    assignment: AdminAssignment,
    role_id: int,
) -> str | None:
    promotion = (
        await session.execute(
            select(TelegramAdminPromotion)
            .where(
                TelegramAdminPromotion.chat_id == chat_id,
                TelegramAdminPromotion.user_id == assignment.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if promotion is not None:
        try:
            member = await callback.bot.get_chat_member(chat_id, assignment.user_id)
        except Exception:
            member = None
        if member is not None and member.status == "administrator":
            rights = {
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
            error = await _check_bot_promotion_rights(callback.bot, chat_id, {})
            if error:
                return error
            try:
                await callback.bot.promote_chat_member(
                    chat_id=chat_id,
                    user_id=assignment.user_id,
                    is_anonymous=False,
                    **rights,
                )
            except Exception:
                return "Telegram не позволил снять права администратора. Проверьте право Mimorus назначать администраторов."
        await session.delete(promotion)

    target_id = assignment.user_id
    if assignment.is_reserve:
        assignment.role_id = None
    else:
        await session.delete(assignment)
    await write_audit(
        session,
        "group.admin_rank_removed",
        chat_id=chat_id,
        actor_user_id=callback.from_user.id,
        target_type="user",
        target_id=str(target_id),
        payload={"role_id": role_id},
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
                await upsert_user(session, callback.from_user)

        target_text = clickable_user_display(user)
        actor_text = clickable_identity(
            telegram_user_id=callback.from_user.id,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            username=callback.from_user.username,
        )
        text = (
            "👑 <b>Ранг назначен</b>\n\n"
            f"Пользователь: {target_text}\n"
            f"Ранг: <b>{escape(role.name)}</b>\n"
            f"Назначил: {actor_text}"
        )
        if callback.message is not None:
            await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer("Ранг назначен")

    return router
