from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole, Group, User
from groupbot.routers.group_control import (
    AdminRoleState,
    KNOWN_PERMISSIONS,
    _ensure_group_settings,
    _owner_access,
    _trial_rank_limit,
)
from groupbot.routers.member_status_guard import is_regular_group_member
from groupbot.services.audit import write_audit
from groupbot.services.special_statuses import special_status_ids
from groupbot.services.users import upsert_user


# Ordered from highest to lowest. Assignment limits are approved project rules.
STANDARD_RANKS: list[tuple[str, str, int | None]] = [
    ("deputy_owner", "Зам. владельца", 1),
    ("chief_admin", "Глав. админ", 2),
    ("chat_admin", "Администратор чата", None),
    ("voice_admin", "Администратор войса", None),
    ("helper", "Помощник", None),
]
STANDARD_NAMES = {name for _, name, _ in STANDARD_RANKS}
RANK_META = {name: (key, limit, index + 1) for index, (key, name, limit) in enumerate(STANDARD_RANKS)}

SPECIAL_STATUSES = {
    "vip": "💎 VIP",
    "nedotroga": "🛡 Недотрога",
}


class HierarchyState(StatesGroup):
    waiting_rank_user_id = State()
    waiting_special_user_id = State()


def _limit_text(limit: int | None) -> str:
    return "без ограничений" if limit is None else str(limit)


def _user_link(user: User) -> str:
    name = escape((user.first_name or "") + ((" " + user.last_name) if user.last_name else "")) or str(user.telegram_user_id)
    username = f"@{escape(user.username)}" if user.username else None
    left = f'<a href="tg://user?id={user.telegram_user_id}">{name}</a>'
    if username:
        right = f'<a href="tg://user?id={user.telegram_user_id}">{username}</a>'
        return f"{left} | {right}"
    return left


async def _ensure_standard_roles(session: AsyncSession, chat_id: int) -> list[AdminRole]:
    existing = (
        await session.execute(select(AdminRole).where(AdminRole.chat_id == chat_id))
    ).scalars().all()
    by_name = {role.name: role for role in existing}
    for _, name, _ in STANDARD_RANKS:
        if name in by_name:
            continue
        role = AdminRole(chat_id=chat_id, name=name, is_active=True)
        session.add(role)
        await session.flush()
        for key, _ in KNOWN_PERMISSIONS:
            session.add(AdminPermission(role_id=role.id, permission=key, allowed=False))
        by_name[name] = role
    await session.flush()
    return [by_name[name] for _, name, _ in STANDARD_RANKS]


async def _assignment_count(session: AsyncSession, role_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(AdminAssignment).where(AdminAssignment.role_id == role_id)
        )
    ).scalar_one()


def _administration_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Ранги администрации", callback_data=f"gctl:roles:{chat_id}")],
        [InlineKeyboardButton(text="👮 Администраторы", callback_data=f"gctl:admins:{chat_id}")],
        [InlineKeyboardButton(text="💎 VIP / 🛡 Недотрога", callback_data=f"hier:special:{chat_id}")],
        [InlineKeyboardButton(text="🧯 Резервный администратор", callback_data=f"gctl:reserve:{chat_id}")],
        [InlineKeyboardButton(text="🌐 Сетевые администраторы", callback_data=f"gctl:network_admins:{chat_id}")],
        [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
    ])


async def _render_roles(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        async with session.begin():
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return
            standard = await _ensure_standard_roles(session, chat_id)
        all_roles = (
            await session.execute(select(AdminRole).where(AdminRole.chat_id == chat_id).order_by(AdminRole.id))
        ).scalars().all()
        custom = [role for role in all_roles if role.name not in STANDARD_NAMES]
        counts = {role.id: await _assignment_count(session, role.id) for role in standard}

    rows: list[list[InlineKeyboardButton]] = []
    for role in standard:
        _, limit, _ = RANK_META[role.name]
        used = counts[role.id]
        cap = "∞" if limit is None else str(limit)
        rows.append([InlineKeyboardButton(
            text=f"{role.name} — {used}/{cap}"[:64],
            callback_data=f"hier:role:{chat_id}:{role.id}",
        )])
    if custom:
        rows.append([InlineKeyboardButton(text="— Дополнительные ранги —", callback_data="noop")])
        for role in custom:
            rows.append([InlineKeyboardButton(
                text=f"{'✅' if role.is_active else '⛔'} {role.name}"[:64],
                callback_data=f"gctl:role:{chat_id}:{role.id}",
            )])
    rows.append([InlineKeyboardButton(text="➕ Создать свой ранг", callback_data=f"gctl:role_create:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")])

    if callback.message is not None:
        await callback.message.edit_text(
            "👑 <b>Ранги администрации</b>\n\n"
            "Иерархия:\n"
            "1. Зам. владельца — максимум 1\n"
            "2. Глав. админ — максимум 2\n"
            "3. Администратор чата — без ограничений\n"
            "4. Администратор войса — без ограничений\n"
            "5. Помощник — без ограничений\n\n"
            "Права каждого ранга настраиваются владельцем отдельно. VIP и Недотрога являются отдельными статусами.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


def create_admin_hierarchy_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="admin_hierarchy")

    @router.callback_query(F.data.startswith("group:section:") & F.data.endswith(":administration"))
    async def administration(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                    return
                group = (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()
                await _ensure_standard_roles(session, chat_id)
            assignments = (
                await session.execute(select(func.count()).select_from(AdminAssignment).where(AdminAssignment.chat_id == chat_id))
            ).scalar_one()
        title = group.title if group and group.title else str(chat_id)
        if callback.message is not None:
            await callback.message.edit_text(
                "👮 <b>Администрация</b>\n\n"
                f"Группа: <b>{escape(title)}</b>\n"
                "Стандартных рангов: <b>5</b>\n"
                f"Назначений в Mimorus: <b>{assignments}</b>\n\n"
                "Стандартная иерархия создаётся автоматически. Права рангов настраиваются отдельно.",
                parse_mode="HTML",
                reply_markup=_administration_keyboard(chat_id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:roles:"))
    async def roles(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        await state.clear()
        await _render_roles(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("gctl:role_create:"))
    async def custom_role_create(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            limit = await _trial_rank_limit(session, callback.from_user.id)
            custom_count = (
                await session.execute(
                    select(func.count()).select_from(AdminRole).where(
                        AdminRole.chat_id == chat_id,
                        ~AdminRole.name.in_(STANDARD_NAMES),
                    )
                )
            ).scalar_one()
        if limit is not None and custom_count >= limit:
            await callback.answer(f"На TEST доступно до {limit} дополнительных админ-рангов.", show_alert=True)
            return
        await state.set_state(AdminRoleState.waiting_name)
        await state.update_data(chat_id=chat_id)
        if callback.message is not None:
            await callback.message.answer("Отправьте название нового дополнительного административного ранга (1–128 символов).")
        await callback.answer()

    @router.callback_query(F.data.startswith("hier:role:"))
    async def role_card(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2]); role_id = int(parts[3])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            role = (await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))).scalar_one_or_none()
            if role is None or role.name not in STANDARD_NAMES:
                await callback.answer("Стандартный ранг не найден.", show_alert=True)
                return
            count = await _assignment_count(session, role.id)
            _, limit, position = RANK_META[role.name]
        cap = "∞" if limit is None else str(limit)
        if callback.message is not None:
            await callback.message.edit_text(
                "👑 <b>Стандартный админ-ранг</b>\n\n"
                f"Ранг: <b>{escape(role.name)}</b>\n"
                f"Уровень иерархии: <b>{position}/5</b>\n"
                f"Назначено: <b>{count}/{cap}</b>\n"
                f"Лимит назначений: <b>{_limit_text(limit)}</b>\n\n"
                "Права этого ранга можно настроить отдельно.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🛡 Настроить права", callback_data=f"gctl:role:{chat_id}:{role.id}")],
                    [InlineKeyboardButton(text="➕ Назначить пользователя", callback_data=f"hier:assign:{chat_id}:{role.id}")],
                    [InlineKeyboardButton(text="📋 Назначенные", callback_data=f"hier:assigned:{chat_id}:{role.id}")],
                    [InlineKeyboardButton(text="◀️ Ранги администрации", callback_data=f"gctl:roles:{chat_id}")],
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("hier:assign:"))
    async def assign_start(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2]); role_id = int(parts[3])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            role = (await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))).scalar_one_or_none()
            if role is None or role.name not in STANDARD_NAMES:
                await callback.answer("Ранг не найден.", show_alert=True)
                return
            _, limit, _ = RANK_META[role.name]
            if limit is not None and await _assignment_count(session, role_id) >= limit:
                await callback.answer(f"Для ранга «{role.name}» достигнут лимит назначений: {limit}.", show_alert=True)
                return
        await state.set_state(HierarchyState.waiting_rank_user_id)
        await state.update_data(rank_chat_id=chat_id, rank_role_id=role_id)
        if callback.message is not None:
            await callback.message.answer("Отправьте Telegram ID пользователя, которому нужно назначить этот ранг.")
        await callback.answer()

    @router.message(HierarchyState.waiting_rank_user_id, F.chat.type == "private")
    async def assign_user(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            await state.clear()
            return
        try:
            target_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("Нужен числовой Telegram ID пользователя.")
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
            await message.answer("Не удалось подтвердить, что пользователь состоит в группе. Проверьте ID.")
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    await message.answer("Недостаточно прав.")
                    return
                role = (await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))).scalar_one_or_none()
                if role is None or role.name not in STANDARD_NAMES:
                    await state.clear()
                    await message.answer("Ранг не найден.")
                    return
                _, limit, _ = RANK_META[role.name]
                existing = (
                    await session.execute(
                        select(AdminAssignment).where(
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.user_id == target_id,
                        ).with_for_update()
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
        await message.answer(f"✅ Пользователю {target_id} назначен ранг «{role.name}».")

    @router.callback_query(F.data.startswith("hier:assigned:"))
    async def assigned(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2]); role_id = int(parts[3])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            role = (await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))).scalar_one_or_none()
            rows = (
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
        if not rows:
            lines.append("Назначений пока нет.")
        else:
            for assignment, user in rows:
                lines.append(f"• {_user_link(user)}\n  ID: <code>{user.telegram_user_id}</code>")
                keyboard.append([InlineKeyboardButton(
                    text=f"❌ Снять: {user.username or user.telegram_user_id}"[:64],
                    callback_data=f"hier:remove:{chat_id}:{assignment.id}:{role_id}",
                )])
        keyboard.append([InlineKeyboardButton(text="◀️ К рангу", callback_data=f"hier:role:{chat_id}:{role_id}")])
        if callback.message is not None:
            await callback.message.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("hier:remove:"))
    async def remove_assignment(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2]); assignment_id = int(parts[3]); role_id = int(parts[4])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                row = (
                    await session.execute(
                        select(AdminAssignment).where(
                            AdminAssignment.id == assignment_id,
                            AdminAssignment.chat_id == chat_id,
                            AdminAssignment.role_id == role_id,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is not None:
                    target_id = row.user_id
                    await session.delete(row)
                    await write_audit(
                        session,
                        "group.admin_rank_removed",
                        chat_id=chat_id,
                        actor_user_id=callback.from_user.id,
                        target_type="user",
                        target_id=str(target_id),
                        payload={"role_id": role_id},
                    )
        callback.data = f"hier:assigned:{chat_id}:{role_id}"
        await assigned(callback)

    @router.callback_query(F.data.startswith("hier:special:"))
    async def special_screen(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            cfg = dict(settings.moderation_config or {})
            vip = special_status_ids(cfg, "vip")
            ned = special_status_ids(cfg, "nedotroga")
        if callback.message is not None:
            await callback.message.edit_text(
                "💎 <b>Особые статусы</b>\n\n"
                f"VIP: <b>{len(vip)}</b> — отдельный статус иммунитета.\n"
                f"Недотрога: <b>{len(ned)}</b> — назначений без ограничения.\n\n"
                "Эти статусы не входят в административную иерархию.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 VIP", callback_data=f"hier:special_list:{chat_id}:vip")],
                    [InlineKeyboardButton(text="🛡 Недотрога", callback_data=f"hier:special_list:{chat_id}:nedotroga")],
                    [InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")],
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("hier:special_list:"))
    async def special_list(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2]); status = parts[3]
        except (ValueError, IndexError):
            return
        if status not in SPECIAL_STATUSES:
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            cfg = dict(settings.moderation_config or {})
            ids = sorted(special_status_ids(cfg, status))
            users = []
            if ids:
                users = (await session.execute(select(User).where(User.telegram_user_id.in_(ids)))).scalars().all()
            by_id = {u.telegram_user_id: u for u in users}
        lines = [f"{SPECIAL_STATUSES[status]} — <b>назначенные</b>", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not ids:
            lines.append("Назначений пока нет.")
        else:
            for uid in ids:
                user = by_id.get(uid)
                lines.append(f"• {_user_link(user) if user else uid}\n  ID: <code>{uid}</code>")
                rows.append([InlineKeyboardButton(
                    text=f"❌ Снять {uid}",
                    callback_data=f"hier:special_remove:{chat_id}:{status}:{uid}",
                )])
        rows.append([InlineKeyboardButton(text="➕ Назначить", callback_data=f"hier:special_add:{chat_id}:{status}")])
        rows.append([InlineKeyboardButton(text="◀️ Особые статусы", callback_data=f"hier:special:{chat_id}")])
        if callback.message is not None:
            await callback.message.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("hier:special_add:"))
    async def special_add(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2]); status = parts[3]
        except (ValueError, IndexError):
            return
        if status not in SPECIAL_STATUSES:
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
        await state.set_state(HierarchyState.waiting_special_user_id)
        await state.update_data(special_chat_id=chat_id, special_status=status)
        if callback.message is not None:
            await callback.message.answer(f"Отправьте Telegram ID обычного участника для статуса {SPECIAL_STATUSES[status]}.")
        await callback.answer()

    @router.message(HierarchyState.waiting_special_user_id, F.chat.type == "private")
    async def special_add_user(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            await state.clear()
            return
        try:
            target_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("Нужен числовой Telegram ID пользователя.")
            return
        data = await state.get_data()
        chat_id = int(data["special_chat_id"])
        status = str(data["special_status"])
        if status not in SPECIAL_STATUSES:
            await state.clear()
            await message.answer("Неизвестный особый статус.")
            return
        try:
            member = await bot.get_chat_member(chat_id, target_id)
            if not await is_regular_group_member(bot, chat_id, target_id):
                await message.answer("VIP и Недотрога назначаются только обычным участникам группы.")
                return
        except Exception:
            await message.answer("Не удалось подтвердить обычного участника группы. Проверьте ID.")
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    await message.answer("Недостаточно прав.")
                    return
                await upsert_user(session, member.user)
                settings = await _ensure_group_settings(session, chat_id)
                cfg = dict(settings.moderation_config or {})
                statuses = dict(cfg.get("special_statuses") or {})
                ids = special_status_ids(cfg, status)
                ids.add(target_id)
                statuses[status] = sorted(ids)
                cfg["special_statuses"] = statuses
                settings.moderation_config = cfg
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
        await message.answer(f"✅ Статус {SPECIAL_STATUSES[status]} назначен пользователю {target_id}.")

    @router.callback_query(F.data.startswith("hier:special_remove:"))
    async def special_remove(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2]); status = parts[3]; target_id = int(parts[4])
        except (ValueError, IndexError):
            return
        if status not in SPECIAL_STATUSES:
            await callback.answer("Неизвестный особый статус.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = dict(settings.moderation_config or {})
                statuses = dict(cfg.get("special_statuses") or {})
                ids = special_status_ids(cfg, status)
                ids.discard(target_id)
                statuses[status] = sorted(ids)
                cfg["special_statuses"] = statuses
                settings.moderation_config = cfg
                await write_audit(
                    session,
                    "group.special_status_removed",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="user",
                    target_id=str(target_id),
                    payload={"status": status},
                )
        callback.data = f"hier:special_list:{chat_id}:{status}"
        await special_list(callback)

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    return router
