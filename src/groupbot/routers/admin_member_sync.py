from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupMember, MemberStatus
from groupbot.routers.admin_hierarchy import RANK_META, SPECIAL_STATUSES, STANDARD_NAMES, _assignment_count
from groupbot.routers.group_control import _owner_access
from groupbot.routers.identity_privacy import _known_group_users, _user_picker_keyboard
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


def create_admin_member_sync_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="admin_member_sync")

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

            from groupbot.models import AdminRole
            from sqlalchemy import select

            role = (
                await session.execute(
                    select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            if role is None or role.name not in STANDARD_NAMES:
                await callback.answer("Ранг не найден.", show_alert=True)
                return

            _, limit, _ = RANK_META[role.name]
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
                "Выберите участника по имени или username.\n"
                "Telegram ID используется только внутри Mimorus."
            )
            if synced:
                text += f"\n\nАдминистраторы Telegram синхронизированы: <b>{synced}</b>."
            if not users:
                text += (
                    "\n\nПока нет известных участников. Обычные участники появляются здесь "
                    "после любой активности в группе."
                )
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_user_picker_keyboard(
                    users,
                    f"priv:rank_pick:{chat_id}:{role_id}",
                    f"hier:role:{chat_id}:{role_id}",
                ),
            )
        await callback.answer()

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
                "Выберите участника по имени или username."
            )
            if synced:
                text += f"\n\nАдминистраторы Telegram синхронизированы: <b>{synced}</b>."
            if not users:
                text += "\n\nПока нет известных участников."
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_user_picker_keyboard(
                    users,
                    f"priv:special_pick:{chat_id}:{status}",
                    f"hier:special_list:{chat_id}:{status}",
                ),
            )
        await callback.answer()

    return router
