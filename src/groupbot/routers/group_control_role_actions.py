from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole
from groupbot.services.audit import write_audit
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_owner


KNOWN_PERMISSIONS = [
    ("warning", "⚠️ Предупреждение"),
    ("mute", "🔇 Мут"),
    ("ban", "⛔ Бан"),
    ("unmute", "🔊 Размут"),
    ("unban", "✅ Разбан"),
    ("delete", "🗑 Удаление сообщений"),
    ("pin", "📌 Закрепление сообщений"),
    ("stats", "📊 Полная статистика"),
]


async def _owner_access(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    return await is_group_owner(session, chat_id, user_id) and await active_subscription_for_owner(session, user_id) is not None


def _keyboard(chat_id: int, role: AdminRole, permissions: dict[str, bool]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, title in KNOWN_PERMISSIONS:
        rows.append([
            InlineKeyboardButton(
                text=f"{'✅' if permissions.get(key, False) else '❌'} {title}",
                callback_data=f"gctl:perm:{chat_id}:{role.id}:{key}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="⛔ Выключить ранг" if role.is_active else "✅ Включить ранг",
            callback_data=f"gctl:role_toggle:{chat_id}:{role.id}",
        )
    ])
    rows.append([InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_role(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    role_id: int,
) -> None:
    async with session_factory() as session:
        role = (
            await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))
        ).scalar_one_or_none()
        perm_rows = (
            await session.execute(select(AdminPermission).where(AdminPermission.role_id == role_id))
        ).scalars().all()
        assignments = (
            await session.execute(select(func.count()).select_from(AdminAssignment).where(AdminAssignment.role_id == role_id))
        ).scalar_one()
    if role is None or callback.message is None:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    permissions = {row.permission: row.allowed for row in perm_rows}
    await callback.message.edit_text(
        "👑 <b>Админ-ранг</b>\n\n"
        f"Название: <b>{role.name}</b>\n"
        f"Статус: {'✅ включён' if role.is_active else '⛔ выключен'}\n"
        f"Назначено пользователей: <b>{assignments}</b>\n\n"
        "Права включаются владельцем индивидуально:",
        parse_mode="HTML",
        reply_markup=_keyboard(chat_id, role, permissions),
    )
    await callback.answer("Сохранено")


def create_group_control_role_actions_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_control_role_actions")

    @router.callback_query(F.data.startswith("gctl:perm:"))
    async def toggle_permission(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        chat_id = int(parts[2])
        role_id = int(parts[3])
        permission = parts[4]
        if permission not in {key for key, _ in KNOWN_PERMISSIONS}:
            await callback.answer("Неизвестное право.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))
                ).scalar_one_or_none()
                if role is None:
                    await callback.answer("Ранг не найден.", show_alert=True)
                    return
                row = (
                    await session.execute(
                        select(AdminPermission).where(
                            AdminPermission.role_id == role_id,
                            AdminPermission.permission == permission,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = AdminPermission(role_id=role_id, permission=permission, allowed=True)
                    session.add(row)
                    value = True
                else:
                    row.allowed = not row.allowed
                    value = row.allowed
                await write_audit(
                    session,
                    "group.admin_permission_changed",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"permission": permission, "allowed": value},
                )
        await _render_role(callback, session_factory, chat_id, role_id)

    @router.callback_query(F.data.startswith("gctl:role_toggle:"))
    async def toggle_role(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        chat_id = int(parts[2])
        role_id = int(parts[3])
        async with session_factory() as session:
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
                role.is_active = not role.is_active
                value = role.is_active
                await write_audit(
                    session,
                    "group.admin_role_toggled",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"is_active": value},
                )
        await _render_role(callback, session_factory, chat_id, role_id)

    return router
