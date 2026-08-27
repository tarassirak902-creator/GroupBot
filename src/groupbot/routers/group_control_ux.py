from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole
from groupbot.routers.admin_member_sync import _remove_role_and_managed_telegram_admin
from groupbot.routers.group_control import (
    KNOWN_PERMISSIONS,
    STANDARD_ADMIN_ROLE_NAMES,
    _custom_rank_count,
    _owner_access,
    _rank_limit,
)
from groupbot.routers.group_control_role_actions import (
    _sync_managed_telegram_admins_for_role,
    _sync_managed_telegram_admins_for_role_state,
)
from groupbot.services.audit import write_audit


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
    rows.append([InlineKeyboardButton(text="➕ Создать свой ранг", callback_data=f"gctl:role_create:{chat_id}")])
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
    rows.append([InlineKeyboardButton(text="🗑 Удалить ранг", callback_data=f"gctl:role_delete:{chat_id}:{role_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _role_delete_keyboard(chat_id: int, role_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"gctl:role_delete_confirm:{chat_id}:{role_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"gctl:role:{chat_id}:{role_id}")],
    ])


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
) -> None:
    await target.edit_text(
        "👑 <b>Настройка админ-ранга</b>\n\n"
        f"Название: <b>{role.name}</b>\n"
        f"Статус: {'✅ включён' if role.is_active else '⛔ выключен'}\n"
        f"Назначено пользователей: <b>{assignments}</b>\n\n"
        "Выберите нужные разрешения. Изменения применятся только после нажатия <b>💾 Сохранить</b>.",
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
            rows = list((
                await session.execute(
                    select(AdminRole).where(AdminRole.chat_id == chat_id).order_by(AdminRole.id)
                )
            ).scalars().all())
            limit = await _rank_limit(session, callback.from_user.id)
            custom_count = await _custom_rank_count(session, chat_id)
        suffix = f"\nЛимит тарифа с дополнениями: <b>{limit}</b> ранга." if limit is not None else ""
        if callback.message is not None:
            await callback.message.edit_text(
                "👑 <b>Ранги администрации</b>\n\n"
                f"Собственных рангов: <b>{custom_count}</b>.{suffix}\n"
                "Стандартные ранги не входят в этот лимит. Выберите ранг для настройки или создайте свой.",
                parse_mode="HTML",
                reply_markup=_roles_keyboard(chat_id, rows),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:role_delete_confirm:"))
    async def role_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return

        removal_error: str | None = None
        role_name = ""
        assignments = 0
        async with session_factory() as session:
            try:
                async with session.begin():
                    if not await _owner_access(session, chat_id, callback.from_user.id):
                        await callback.answer("Недостаточно прав.", show_alert=True)
                        return
                    role = (
                        await session.execute(
                            select(AdminRole)
                            .where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if role is None:
                        await callback.answer("Ранг уже удалён.", show_alert=True)
                        return
                    if role.name in STANDARD_ADMIN_ROLE_NAMES:
                        await callback.answer("Стандартный ранг Mimorus удалить нельзя.", show_alert=True)
                        return

                    role_name = role.name
                    assignment_rows = list((
                        await session.execute(
                            select(AdminAssignment)
                            .where(AdminAssignment.role_id == role_id)
                            .with_for_update()
                        )
                    ).scalars().all())
                    assignments = len(assignment_rows)

                    for assignment in assignment_rows:
                        error = await _remove_role_and_managed_telegram_admin(
                            callback,
                            session,
                            chat_id=chat_id,
                            assignment=assignment,
                            role_id=role_id,
                        )
                        if error:
                            raise RuntimeError(error)

                    await session.execute(delete(AdminRole).where(AdminRole.id == role_id))
                    await write_audit(
                        session,
                        "group.admin_role_deleted",
                        chat_id=chat_id,
                        actor_user_id=callback.from_user.id,
                        target_type="admin_role",
                        target_id=str(role_id),
                        payload={"name": role_name, "assignments_removed": assignments},
                    )
            except RuntimeError as exc:
                removal_error = str(exc)

        if removal_error:
            await callback.answer(removal_error, show_alert=True)
            return

        await state.clear()
        async with session_factory() as session:
            rows = list((
                await session.execute(
                    select(AdminRole).where(AdminRole.chat_id == chat_id).order_by(AdminRole.id)
                )
            ).scalars().all())
        if callback.message is not None:
            await callback.message.edit_text(
                f"✅ Ранг «{role_name}» удалён.\n\nНазначения этого ранга сняты: <b>{assignments}</b>.",
                parse_mode="HTML",
                reply_markup=_roles_keyboard(chat_id, rows),
            )
        await callback.answer("Ранг удалён")

    @router.callback_query(F.data.startswith("gctl:role_delete:"))
    async def role_delete(callback: CallbackQuery) -> None:
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
            role, _, assignments = await _load_role(session, chat_id, role_id)
        if role is None:
            await callback.answer("Ранг не найден.", show_alert=True)
            return
        if role.name in STANDARD_ADMIN_ROLE_NAMES:
            await callback.answer("Стандартный ранг Mimorus удалить нельзя.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(
                "⚠️ <b>Удалить дополнительный административный ранг?</b>\n\n"
                f"Ранг: <b>{role.name}</b>\n"
                f"Назначено пользователей: <b>{assignments}</b>\n\n"
                "После подтверждения ранг будет удалён. Пользователи с этим рангом потеряют его назначение.",
                parse_mode="HTML",
                reply_markup=_role_delete_keyboard(chat_id, role_id),
            )
        await callback.answer()

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

        sync_error: str | None = None
        saved: dict[str, bool] = {}
        role_name = ""
        role_active = False
        async with session_factory() as session:
            try:
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
                    role_name = role.name
                    role_active = role.is_active
                    rows = (
                        await session.execute(
                            select(AdminPermission).where(AdminPermission.role_id == role_id).with_for_update()
                        )
                    ).scalars().all()
                    by_key = {row.permission: row for row in rows}
                    for key, _ in KNOWN_PERMISSIONS:
                        value = bool(draft.get(key, False))
                        saved[key] = value
                        row = by_key.get(key)
                        if row is None:
                            session.add(AdminPermission(role_id=role_id, permission=key, allowed=value))
                        else:
                            row.allowed = value

                    await session.flush()
                    if role_active:
                        sync_error = await _sync_managed_telegram_admins_for_role(
                            callback,
                            session,
                            chat_id=chat_id,
                            role_id=role_id,
                        )
                        if sync_error:
                            raise RuntimeError(sync_error)

                    await write_audit(
                        session,
                        "group.admin_permissions_saved",
                        chat_id=chat_id,
                        actor_user_id=callback.from_user.id,
                        target_type="admin_role",
                        target_id=str(role_id),
                        payload={"permissions": saved, "telegram_admins_synced": role_active},
                    )
            except RuntimeError as exc:
                sync_error = str(exc)

        if sync_error:
            await callback.answer(sync_error, show_alert=True)
            return

        await state.clear()
        await state.update_data(
            permission_draft_chat_id=chat_id,
            permission_draft_role_id=role_id,
            permission_draft=saved,
        )
        async with session_factory() as session:
            _, _, assignments = await _load_role(session, chat_id, role_id)
        if role_active:
            result_text = (
                "✅ <b>Разрешения сохранены</b>\n\n"
                f"Ранг: <b>{role_name}</b>\n"
                "Настройки применены и синхронизированы с Telegram для администраторов, назначенных Mimorus."
            )
        else:
            result_text = (
                "✅ <b>Разрешения сохранены</b>\n\n"
                f"Ранг: <b>{role_name}</b>\n"
                "Настройки сохранены. Ранг выключен — Telegram-права будут применены при его включении."
            )
        if callback.message is not None:
            await callback.message.edit_text(
                result_text,
                parse_mode="HTML",
                reply_markup=_permission_editor_keyboard(
                    chat_id,
                    role_id,
                    saved,
                    role_active=role_active,
                ),
            )
        await callback.answer("Разрешения сохранены")

    @router.callback_query(F.data.startswith("gctl:role_toggle:"))
    async def role_toggle(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return

        sync_error: str | None = None
        async with session_factory() as session:
            try:
                async with session.begin():
                    if not await _owner_access(session, chat_id, callback.from_user.id):
                        await callback.answer("Недостаточно прав.", show_alert=True)
                        return
                    role = (
                        await session.execute(
                            select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id).with_for_update()
                        )
                    ).scalar_one_or_none()
                    if role is None:
                        await callback.answer("Ранг не найден.", show_alert=True)
                        return

                    new_value = not role.is_active
                    sync_error = await _sync_managed_telegram_admins_for_role_state(
                        callback,
                        session,
                        chat_id=chat_id,
                        role_id=role_id,
                        enabled=new_value,
                    )
                    if sync_error:
                        raise RuntimeError(sync_error)

                    role.is_active = new_value
                    await write_audit(
                        session,
                        "group.admin_role_toggled",
                        chat_id=chat_id,
                        actor_user_id=callback.from_user.id,
                        target_type="admin_role",
                        target_id=str(role_id),
                        payload={"is_active": new_value, "telegram_admins_synced": True},
                    )
            except RuntimeError as exc:
                sync_error = str(exc)

        if sync_error:
            await callback.answer(sync_error, show_alert=True)
            return

        await state.clear()
        async with session_factory() as session:
            role, permissions, assignments = await _load_role(session, chat_id, role_id)
        if role is not None and callback.message is not None:
            await state.update_data(
                permission_draft_chat_id=chat_id,
                permission_draft_role_id=role_id,
                permission_draft=permissions,
            )
            await _render_permission_editor(
                callback.message,
                chat_id=chat_id,
                role=role,
                assignments=assignments,
                permissions=permissions,
            )
        await callback.answer("Статус ранга обновлён")

    return router
