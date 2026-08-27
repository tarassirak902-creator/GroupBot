from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole
from groupbot.routers.admin_member_sync import _check_bot_promotion_rights, _telegram_rights_for_role
from groupbot.services.audit import write_audit
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_owner
from groupbot.telegram_admin_models import TelegramAdminPromotion


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


async def _sync_managed_telegram_admins_for_role(
    callback: CallbackQuery,
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
) -> str | None:
    """Apply current rank rights to Telegram admins promoted by Mimorus.

    Telegram admins that existed before Mimorus promoted them are intentionally
    excluded: their rights belong to the group owner and must not be overwritten.
    """
    target_ids = list((
        await session.execute(
            select(AdminAssignment.user_id)
            .join(
                TelegramAdminPromotion,
                (TelegramAdminPromotion.chat_id == AdminAssignment.chat_id)
                & (TelegramAdminPromotion.user_id == AdminAssignment.user_id),
            )
            .where(
                AdminAssignment.chat_id == chat_id,
                AdminAssignment.role_id == role_id,
            )
        )
    ).scalars().all())
    if not target_ids:
        return None

    rights = await _telegram_rights_for_role(session, role_id)
    error = await _check_bot_promotion_rights(callback.bot, chat_id, rights)
    if error:
        return error

    for target_id in target_ids:
        try:
            member = await callback.bot.get_chat_member(chat_id, target_id)
        except Exception:
            return f"Не удалось проверить администратора Telegram ID {target_id}. Изменение права отменено."
        if member.status != "administrator":
            continue
        try:
            await callback.bot.promote_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                is_anonymous=False,
                **rights,
            )
        except Exception:
            return (
                "Telegram не позволил обновить права одного из администраторов. "
                "Изменение права ранга отменено. Проверьте права Mimorus."
            )
    return None


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
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        permission = parts[4]
        if permission not in {key for key, _ in KNOWN_PERMISSIONS}:
            await callback.answer("Неизвестное право.", show_alert=True)
            return

        sync_error: str | None = None
        async with session_factory() as session:
            try:
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

                    await session.flush()
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
                        "group.admin_permission_changed",
                        chat_id=chat_id,
                        actor_user_id=callback.from_user.id,
                        target_type="admin_role",
                        target_id=str(role_id),
                        payload={"permission": permission, "allowed": value},
                    )
            except RuntimeError as exc:
                sync_error = str(exc)

        if sync_error:
            await callback.answer(sync_error, show_alert=True)
            return
        await _render_role(callback, session_factory, chat_id, role_id)

    @router.callback_query(F.data.startswith("gctl:role_toggle:"))
    async def toggle_role(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
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
