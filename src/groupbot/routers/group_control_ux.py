from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole, Group, GroupSettings
from groupbot.routers.group_control import (
    AdminRoleState,
    KNOWN_PERMISSIONS,
    _ensure_group_settings,
    _owner_access,
    _trial_rank_limit,
)
from groupbot.services.audit import write_audit


MODE_DESCRIPTIONS = (
    "Текстовый — действие и причина пишутся ответом на сообщение.\n"
    "Кнопки — после команды бот предлагает срок/причину.\n"
    "Оба режима — работают оба варианта."
)


def _mode_keyboard(chat_id: int, current: str) -> InlineKeyboardMarkup:
    def label(value: str, text: str) -> str:
        return ("✅ " if current == value else "▫️ ") + text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("text", "Текстовый"), callback_data=f"gctl:setmode:{chat_id}:text")],
            [InlineKeyboardButton(text=label("buttons", "Кнопки"), callback_data=f"gctl:setmode:{chat_id}:buttons")],
            [InlineKeyboardButton(text=label("both", "Оба режима"), callback_data=f"gctl:setmode:{chat_id}:both")],
            [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
        ]
    )


def _admin_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👑 Ранги администрации", callback_data=f"gctl:roles:{chat_id}")],
            [InlineKeyboardButton(text="👮 Администраторы", callback_data=f"gctl:admins:{chat_id}")],
            [InlineKeyboardButton(text="🧯 Резервный администратор", callback_data=f"gctl:reserve:{chat_id}")],
            [InlineKeyboardButton(text="🌐 Сетевые администраторы", callback_data=f"gctl:network_admins:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
        ]
    )


def _roles_keyboard(chat_id: int, roles: list[AdminRole]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for role in roles:
        icon = "✅" if role.is_active else "⛔"
        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {role.name}"[:64],
                callback_data=f"gctl:role:{chat_id}:{role.id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="➕ Создать ранг", callback_data=f"gctl:role_create:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _permission_editor_keyboard(
    chat_id: int,
    role_id: int,
    permissions: dict[str, bool],
    *,
    role_active: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, title in KNOWN_PERMISSIONS:
        icon = "✅" if permissions.get(key, False) else "❌"
        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {title}",
                callback_data=f"gctl:perm:{chat_id}:{role_id}:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"gctl:perm_save:{chat_id}:{role_id}")])
    rows.append([
        InlineKeyboardButton(
            text="⛔ Выключить ранг" if role_active else "✅ Включить ранг",
            callback_data=f"gctl:role_toggle:{chat_id}:{role_id}",
        )
    ])
    rows.append([InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_role(
    session: AsyncSession,
    chat_id: int,
    role_id: int,
) -> tuple[AdminRole | None, dict[str, bool], int]:
    role = (
        await session.execute(
            select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    perm_rows = (
        await session.execute(select(AdminPermission).where(AdminPermission.role_id == role_id))
    ).scalars().all()
    assignments = (
        await session.execute(
            select(func.count()).select_from(AdminAssignment).where(AdminAssignment.role_id == role_id)
        )
    ).scalar_one()
    return role, {row.permission: row.allowed for row in perm_rows}, assignments


async def _render_permission_editor(
    target: Message,
    *,
    chat_id: int,
    role: AdminRole,
    assignments: int,
    permissions: dict[str, bool],
    created: bool = False,
) -> None:
    intro = (
        f"✅ Ранг «{role.name}» создан.\n\nТеперь выберите разрешения и нажмите <b>💾 Сохранить</b>."
        if created
        else "Выберите нужные разрешения. Изменения применятся только после нажатия <b>💾 Сохранить</b>."
    )
    await target.edit_text(
        "👑 <b>Настройка админ-ранга</b>\n\n"
        f"Название: <b>{role.name}</b>\n"
        f"Статус: {'✅ включён' if role.is_active else '⛔ выключен'}\n"
        f"Назначено пользователей: <b>{assignments}</b>\n\n"
        f"{intro}",
        parse_mode="HTML",
        reply_markup=_permission_editor_keyboard(
            chat_id,
            role.id,
            permissions,
            role_active=role.is_active,
        ),
    )


def create_group_control_ux_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_control_ux")

    @router.callback_query(F.data.startswith("group:section:") & F.data.endswith(":administration"))
    async def administration(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return
            group = (
                await session.execute(select(Group).where(Group.chat_id == chat_id))
            ).scalar_one_or_none()
            roles_count = (
                await session.execute(
                    select(func.count()).select_from(AdminRole).where(AdminRole.chat_id == chat_id)
                )
            ).scalar_one()
            assignments_count = (
                await session.execute(
                    select(func.count()).select_from(AdminAssignment).where(AdminAssignment.chat_id == chat_id)
                )
            ).scalar_one()
        if callback.message is not None:
            title = group.title if group and group.title else str(chat_id)
            await callback.message.edit_text(
                "👮 <b>Администрация</b>\n\n"
                f"Группа: <b>{title}</b>\n"
                f"Собственных рангов: <b>{roles_count}</b>\n"
                f"Назначений в Mimorus: <b>{assignments_count}</b>\n\n"
                "Владелец создаёт ранг, выбирает его разрешения и сохраняет настройки.",
                parse_mode="HTML",
                reply_markup=_admin_keyboard(chat_id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:mode:"))
    async def mode_screen(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            current = (settings.moderation_config or {}).get("admin_command_mode", "both")
        if callback.message is not None:
            await callback.message.edit_text(
                "🎚 <b>Режим админ-команд</b>\n\n"
                f"{MODE_DESCRIPTIONS}",
                parse_mode="HTML",
                reply_markup=_mode_keyboard(chat_id, current),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:setmode:"))
    async def set_mode(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            mode = parts[3]
        except (ValueError, IndexError):
            return
        if mode not in {"text", "buttons", "both"}:
            await callback.answer("Некорректный режим.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                config = dict(settings.moderation_config or {})
                config["admin_command_mode"] = mode
                settings.moderation_config = config
                await write_audit(
                    session,
                    "group.moderation_mode_changed",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="group",
                    target_id=str(chat_id),
                    payload={"mode": mode},
                )
        if callback.message is not None:
            await callback.message.edit_text(
                "🎚 <b>Режим админ-команд</b>\n\n"
                f"{MODE_DESCRIPTIONS}\n\n"
                "✅ Выбранный режим сохранён.",
                parse_mode="HTML",
                reply_markup=_mode_keyboard(chat_id, mode),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("gctl:roles:"))
    async def roles(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        await state.clear()
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            rows = (
                await session.execute(
                    select(AdminRole).where(AdminRole.chat_id == chat_id).order_by(AdminRole.id)
                )
            ).scalars().all()
            limit = await _trial_rank_limit(session, callback.from_user.id)
        suffix = f"\nЛимит TEST: <b>{limit}</b> ранга." if limit is not None else ""
        if callback.message is not None:
            await callback.message.edit_text(
                "👑 <b>Ранги администрации</b>\n\n"
                f"Создано: <b>{len(rows)}</b>.{suffix}\n"
                "Выберите существующий ранг для изменения разрешений или создайте новый.",
                parse_mode="HTML",
                reply_markup=_roles_keyboard(chat_id, list(rows)),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:role_create:"))
    async def role_create(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            limit = await _trial_rank_limit(session, callback.from_user.id)
            count = (
                await session.execute(
                    select(func.count()).select_from(AdminRole).where(AdminRole.chat_id == chat_id)
                )
            ).scalar_one()
        if limit is not None and count >= limit:
            await callback.answer(f"На TEST доступно до {limit} админ-рангов.", show_alert=True)
            return
        await state.set_state(AdminRoleState.waiting_name)
        await state.update_data(chat_id=chat_id)
        if callback.message is not None:
            await callback.message.answer("Отправьте название нового административного ранга (1–128 символов).")
        await callback.answer()

    @router.message(AdminRoleState.waiting_name, F.chat.type == "private")
    async def role_name(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 128:
            await message.answer("Название должно быть длиной 1–128 символов.")
            return
        data = await state.get_data()
        chat_id = int(data["chat_id"])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    await message.answer("Недостаточно прав.")
                    return
                exists = (
                    await session.execute(
                        select(AdminRole.id).where(AdminRole.chat_id == chat_id, AdminRole.name == name)
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    await message.answer("Ранг с таким названием уже существует.")
                    return
                role = AdminRole(chat_id=chat_id, name=name, is_active=True)
                session.add(role)
                await session.flush()
                for key, _ in KNOWN_PERMISSIONS:
                    session.add(AdminPermission(role_id=role.id, permission=key, allowed=False))
                await write_audit(
                    session,
                    "group.admin_role_created",
                    chat_id=chat_id,
                    actor_user_id=message.from_user.id,
                    target_type="admin_role",
                    target_id=str(role.id),
                    payload={"name": name},
                )
            role_id = role.id
        draft = {key: False for key, _ in KNOWN_PERMISSIONS}
        await state.clear()
        await state.update_data(
            permission_draft_chat_id=chat_id,
            permission_draft_role_id=role_id,
            permission_draft=draft,
        )
        await message.answer(
            f"✅ Ранг «{name}» создан.\n\nВыберите разрешения для этого ранга:",
            reply_markup=_permission_editor_keyboard(chat_id, role_id, draft, role_active=True),
        )

    @router.callback_query(F.data.startswith("gctl:role:"))
    async def role_card(callback: CallbackQuery, state: FSMContext) -> None:
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
            role, permissions, assignments = await _load_role(session, chat_id, role_id)
        if role is None:
            await callback.answer("Ранг не найден.", show_alert=True)
            return
        await state.clear()
        await state.update_data(
            permission_draft_chat_id=chat_id,
            permission_draft_role_id=role_id,
            permission_draft=permissions,
        )
        if callback.message is not None:
            await _render_permission_editor(
                callback.message,
                chat_id=chat_id,
                role=role,
                assignments=assignments,
                permissions=permissions,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:perm:"))
    async def toggle_permission(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
            permission = parts[4]
        except (ValueError, IndexError):
            return
        if permission not in {key for key, _ in KNOWN_PERMISSIONS}:
            await callback.answer("Неизвестное право.", show_alert=True)
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            role, persisted, assignments = await _load_role(session, chat_id, role_id)
        if role is None:
            await callback.answer("Ранг не найден.", show_alert=True)
            return

        data = await state.get_data()
        if (
            data.get("permission_draft_chat_id") == chat_id
            and data.get("permission_draft_role_id") == role_id
            and isinstance(data.get("permission_draft"), dict)
        ):
            draft = dict(data["permission_draft"])
        else:
            draft = dict(persisted)
        draft[permission] = not bool(draft.get(permission, False))
        await state.update_data(
            permission_draft_chat_id=chat_id,
            permission_draft_role_id=role_id,
            permission_draft=draft,
        )
        if callback.message is not None:
            await _render_permission_editor(
                callback.message,
                chat_id=chat_id,
                role=role,
                assignments=assignments,
                permissions=draft,
            )
        await callback.answer("Выбрано. Нажмите «Сохранить» для применения.")

    @router.callback_query(F.data.startswith("gctl:perm_save:"))
    async def save_permissions(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return
        data = await state.get_data()
        draft = data.get("permission_draft")
        if not isinstance(draft, dict):
            await callback.answer("Нет несохранённых настроек.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                role = (
                    await session.execute(
                        select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
                    )
                ).scalar_one_or_none()
                if role is None:
                    await callback.answer("Ранг не найден.", show_alert=True)
                    return
                rows = (
                    await session.execute(
                        select(AdminPermission).where(AdminPermission.role_id == role_id).with_for_update()
                    )
                ).scalars().all()
                by_key = {row.permission: row for row in rows}
                saved: dict[str, bool] = {}
                for key, _ in KNOWN_PERMISSIONS:
                    value = bool(draft.get(key, False))
                    saved[key] = value
                    row = by_key.get(key)
                    if row is None:
                        session.add(AdminPermission(role_id=role_id, permission=key, allowed=value))
                    else:
                        row.allowed = value
                await write_audit(
                    session,
                    "group.admin_permissions_saved",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"permissions": saved},
                )
            role_active = role.is_active
        await state.clear()
        await state.update_data(
            permission_draft_chat_id=chat_id,
            permission_draft_role_id=role_id,
            permission_draft=saved,
        )
        async with session_factory() as session:
            _, _, assignments = await _load_role(session, chat_id, role_id)
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>Разрешения сохранены</b>\n\n"
                f"Ранг: <b>{role.name}</b>\n"
                "Настройки применены.",
                parse_mode="HTML",
                reply_markup=_permission_editor_keyboard(
                    chat_id,
                    role_id,
                    saved,
                    role_active=role_active,
                ),
            )
        await callback.answer("Разрешения сохранены")

    return router
