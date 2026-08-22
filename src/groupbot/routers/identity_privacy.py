from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import AdminAssignment, AdminRole, GroupSettings, User
from groupbot.routers.admin_hierarchy import (
    HierarchyState,
    RANK_META,
    SPECIAL_STATUSES,
    STANDARD_NAMES,
    _assignment_count,
)
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.routers.user_display import clickable_identity, clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.subscriptions import active_subscription_for_group, active_subscription_for_owner
from groupbot.services.users import upsert_user
from groupbot.ui import private_main_menu


def _plain_label(user: User) -> str:
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    username = f"@{user.username}" if user.username else ""
    if full_name and username:
        return f"{full_name} | {username}"
    return full_name or username or "Пользователь"


def _special_back(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Особые статусы", callback_data=f"hier:special:{chat_id}")],
    ])


def create_identity_privacy_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    router = Router(name="identity_privacy")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    @router.message(Command("profile"), F.chat.type.in_({"group", "supergroup"}))
    async def group_profile(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            subscription = await active_subscription_for_group(session, message.chat.id)
        if subscription is None:
            return
        identity = clickable_identity(
            telegram_user_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        await message.answer(
            "👤 <b>Профиль</b>\n\n"
            f"Пользователь: {identity}\n\n"
            "Расширенная карточка пользователя будет подключена в блоке статистики и профилей.",
            parse_mode="HTML",
        )

    @router.message(F.chat.type == "private", F.text == "👤 Мой аккаунт")
    async def my_account(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            active = await active_subscription_for_owner(session, message.from_user.id)
        identity = clickable_identity(
            telegram_user_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        await message.answer(
            "👤 <b>Мой аккаунт</b>\n\n"
            f"Пользователь: {identity}\n"
            f"Тариф: {'✅ активен' if active is not None else '❌ не активирован'}",
            parse_mode="HTML",
            reply_markup=private_main_menu(is_creator=is_creator(message.from_user.id)),
        )

    @router.callback_query(F.data == "creator:users")
    async def creator_users(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        async with session_factory() as session:
            users = (
                await session.execute(select(User).order_by(User.updated_at.desc()).limit(30))
            ).scalars().all()
        if callback.message is None:
            return
        lines = ["👤 <b>Пользователи</b>", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not users:
            lines.append("Пользователей пока нет.")
        else:
            for user in users:
                lines.append(f"• {clickable_user_display(user)}")
                rows.append([
                    InlineKeyboardButton(
                        text=f"👤 {_plain_label(user)}"[:64],
                        callback_data=f"creator:usercard:{user.telegram_user_id}",
                    )
                ])
        rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="creator:users")])
        rows.append([InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")])
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("hier:assigned:"))
    async def assigned(callback: CallbackQuery) -> None:
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
                    select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            rows_db = (
                await session.execute(
                    select(AdminAssignment, User)
                    .join(User, User.telegram_user_id == AdminAssignment.user_id)
                    .where(AdminAssignment.chat_id == chat_id, AdminAssignment.role_id == role_id)
                    .order_by(AdminAssignment.id)
                )
            ).all()
        if role is None:
            await callback.answer("Ранг не найден.", show_alert=True)
            return
        lines = [f"📋 <b>{escape(role.name)} — назначенные</b>", ""]
        keyboard: list[list[InlineKeyboardButton]] = []
        if not rows_db:
            lines.append("Назначений пока нет.")
        else:
            for assignment, user in rows_db:
                lines.append(f"• {clickable_user_display(user)}")
                keyboard.append([
                    InlineKeyboardButton(
                        text=f"❌ Снять: {_plain_label(user)}"[:64],
                        callback_data=f"hier:remove:{chat_id}:{assignment.id}:{role_id}",
                    )
                ])
        keyboard.append([InlineKeyboardButton(text="◀️ К рангу", callback_data=f"hier:role:{chat_id}:{role_id}")])
        if callback.message is not None:
            await callback.message.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
        await callback.answer()

    @router.message(HierarchyState.waiting_rank_user_id, F.chat.type == "private")
    async def assign_rank_user(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            await state.clear()
            return
        try:
            target_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("Нужен числовой Telegram ID для внутреннего поиска пользователя.")
            return
        data = await state.get_data()
        chat_id = int(data["rank_chat_id"])
        role_id = int(data["rank_role_id"])
        try:
            member = await bot.get_chat_member(chat_id, target_id)
            if member.status in {"left", "kicked"}:
                await message.answer("Этот пользователь сейчас не состоит в группе.")
                return
        except Exception:
            await message.answer("Не удалось подтвердить участника группы. Проверьте введённый идентификатор.")
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    await message.answer("Недостаточно прав.")
                    return
                role = (
                    await session.execute(
                        select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
                    )
                ).scalar_one_or_none()
                if role is None or role.name not in STANDARD_NAMES:
                    await state.clear()
                    await message.answer("Ранг не найден.")
                    return
                _, limit, _ = RANK_META[role.name]
                existing = (
                    await session.execute(
                        select(AdminAssignment)
                        .where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == target_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                current_count = await _assignment_count(session, role_id)
                if limit is not None and current_count >= limit and (existing is None or existing.role_id != role_id):
                    await message.answer(f"Лимит назначений для «{role.name}» уже достигнут: {limit}.")
                    return
                await upsert_user(session, member.user)
                if existing is None:
                    session.add(AdminAssignment(chat_id=chat_id, user_id=target_id, role_id=role_id, is_reserve=False))
                else:
                    existing.role_id = role_id
                    existing.is_reserve = False
                await write_audit(
                    session,
                    "group.admin_rank_assigned",
                    chat_id=chat_id,
                    actor_user_id=message.from_user.id,
                    target_type="user",
                    target_id=str(target_id),
                    payload={"role_id": role_id, "role_name": role.name},
                )
        await state.clear()
        identity = clickable_identity(
            telegram_user_id=member.user.id,
            first_name=member.user.first_name,
            last_name=member.user.last_name,
            username=member.user.username,
        )
        await message.answer(
            f"✅ Пользователю {identity} назначен ранг «<b>{escape(role.name)}</b>».",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("hier:special_list:"))
    async def special_list(callback: CallbackQuery) -> None:
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
            settings_row = await _ensure_group_settings(session, chat_id)
            cfg = dict(settings_row.moderation_config or {})
            ids = [int(value) for value in list((cfg.get("special_statuses") or {}).get(status) or [])]
            users: list[User] = []
            if ids:
                users = (
                    await session.execute(select(User).where(User.telegram_user_id.in_(ids)))
                ).scalars().all()
            by_id = {user.telegram_user_id: user for user in users}
        lines = [f"{SPECIAL_STATUSES[status]} — <b>назначенные</b>", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not ids:
            lines.append("Назначений пока нет.")
        else:
            for uid in ids:
                user = by_id.get(uid)
                if user is None:
                    lines.append("• Пользователь")
                    button_label = "Пользователь"
                else:
                    lines.append(f"• {clickable_user_display(user)}")
                    button_label = _plain_label(user)
                rows.append([
                    InlineKeyboardButton(
                        text=f"❌ Снять: {button_label}"[:64],
                        callback_data=f"hier:special_remove:{chat_id}:{status}:{uid}",
                    )
                ])
        rows.append([InlineKeyboardButton(text="➕ Назначить", callback_data=f"hier:special_add:{chat_id}:{status}")])
        rows.append([InlineKeyboardButton(text="◀️ Особые статусы", callback_data=f"hier:special:{chat_id}")])
        if callback.message is not None:
            await callback.message.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        await callback.answer()

    @router.message(HierarchyState.waiting_special_user_id, F.chat.type == "private")
    async def special_add_user(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            await state.clear()
            return
        try:
            target_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("Нужен числовой Telegram ID для внутреннего поиска пользователя.")
            return
        data = await state.get_data()
        chat_id = int(data["special_chat_id"])
        status = str(data["special_status"])
        try:
            member = await bot.get_chat_member(chat_id, target_id)
            if member.status in {"left", "kicked"}:
                await message.answer("Этот пользователь сейчас не состоит в группе.")
                return
        except Exception:
            await message.answer("Не удалось подтвердить участника группы. Проверьте введённый идентификатор.")
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    await message.answer("Недостаточно прав.")
                    return
                await upsert_user(session, member.user)
                settings_row = await _ensure_group_settings(session, chat_id)
                cfg = dict(settings_row.moderation_config or {})
                statuses = dict(cfg.get("special_statuses") or {})
                ids = [int(value) for value in list(statuses.get(status) or [])]
                if target_id not in ids:
                    ids.append(target_id)
                statuses[status] = ids
                cfg["special_statuses"] = statuses
                settings_row.moderation_config = cfg
                await write_audit(
                    session,
                    "group.special_status_added",
                    chat_id=chat_id,
                    actor_user_id=message.from_user.id,
                    target_type="user",
                    target_id=str(target_id),
                    payload={"status": status},
                )
        await state.clear()
        identity = clickable_identity(
            telegram_user_id=member.user.id,
            first_name=member.user.first_name,
            last_name=member.user.last_name,
            username=member.user.username,
        )
        await message.answer(
            f"✅ Статус {SPECIAL_STATUSES[status]} назначен пользователю {identity}.",
            parse_mode="HTML",
        )

    return router
