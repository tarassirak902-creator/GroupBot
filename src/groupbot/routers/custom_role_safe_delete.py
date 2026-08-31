from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminRole
from groupbot.routers.admin_member_sync import _remove_role_and_managed_telegram_admin
from groupbot.routers.group_control import STANDARD_ADMIN_ROLE_NAMES, _owner_access
from groupbot.services.audit import write_audit


def create_custom_role_safe_delete_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="custom_role_safe_delete")

    @router.callback_query(F.data.startswith("gctl:role_delete_confirm:"))
    async def delete_custom_role(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return

        role_name = ""
        # Phase 1: freeze the role. Assignment code locks the same row and
        # refuses inactive roles, so no new assignment can enter the deletion.
        async with session_factory() as session:
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
                    await state.clear()
                    await callback.answer("Ранг уже удалён.", show_alert=True)
                    return
                if role.name in STANDARD_ADMIN_ROLE_NAMES:
                    await callback.answer("Стандартный ранг Mimorus удалить нельзя.", show_alert=True)
                    return
                role_name = role.name
                role.is_active = False
                await write_audit(
                    session,
                    "group.admin_role_delete_started",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"name": role_name},
                )

        removed = 0
        failure: str | None = None

        # Phase 2: each Telegram/DB removal commits independently. A Telegram
        # failure cannot roll back already-applied Telegram changes for earlier
        # users and leave their DB assignments falsely active.
        while True:
            async with session_factory() as session:
                async with session.begin():
                    assignment = (
                        await session.execute(
                            select(AdminAssignment)
                            .where(
                                AdminAssignment.chat_id == chat_id,
                                AdminAssignment.role_id == role_id,
                            )
                            .order_by(AdminAssignment.id.asc())
                            .limit(1)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if assignment is None:
                        break
                    error = await _remove_role_and_managed_telegram_admin(
                        callback,
                        session,
                        chat_id=chat_id,
                        assignment=assignment,
                        role_id=role_id,
                    )
                    if error:
                        failure = error
                        # Force rollback of only this user's transaction.
                        await session.rollback()
                        break
                    removed += 1

            if failure:
                break

        if failure:
            await state.clear()
            if callback.message is not None:
                await callback.message.edit_text(
                    "⚠️ <b>Удаление ранга остановлено</b>\n\n"
                    f"Ранг: <b>{role_name}</b>\n"
                    f"Уже безопасно снято назначений: <b>{removed}</b>.\n\n"
                    f"Причина остановки: {failure}\n\n"
                    "Ранг оставлен выключенным, поэтому оставшиеся назначения не дают Mimorus-права. "
                    "Исправьте права Mimorus в Telegram и повторите удаление.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Повторить удаление", callback_data=f"gctl:role_delete_confirm:{chat_id}:{role_id}")],
                        [InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")],
                    ]),
                )
            await callback.answer("Удаление остановлено безопасно", show_alert=True)
            return

        # Phase 3: only an empty, frozen role is deleted.
        async with session_factory() as session:
            async with session.begin():
                role = (
                    await session.execute(
                        select(AdminRole)
                        .where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if role is not None:
                    remaining = (
                        await session.execute(
                            select(AdminAssignment.id)
                            .where(
                                AdminAssignment.chat_id == chat_id,
                                AdminAssignment.role_id == role_id,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if remaining is not None:
                        await callback.answer(
                            "Появилось новое назначение. Повторите удаление.",
                            show_alert=True,
                        )
                        return
                    await session.execute(delete(AdminRole).where(AdminRole.id == role_id))
                    await write_audit(
                        session,
                        "group.admin_role_deleted",
                        chat_id=chat_id,
                        actor_user_id=callback.from_user.id,
                        target_type="admin_role",
                        target_id=str(role_id),
                        payload={"name": role_name, "assignments_removed": removed, "safe_delete": True},
                    )

        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text(
                f"✅ Ранг «{role_name}» удалён.\n\nНазначения этого ранга сняты: <b>{removed}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")],
                ]),
            )
        await callback.answer("Ранг удалён")

    return router
