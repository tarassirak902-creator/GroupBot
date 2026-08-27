from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, AdminPermission, AdminRole
from groupbot.routers.admin_hierarchy import RANK_META, STANDARD_NAMES
from groupbot.routers.group_control import KNOWN_PERMISSIONS, _owner_access
from groupbot.services.helper_role_policy import (
    HELPER_ROLE,
    _standard_telegram_rights,
)


class StandardAdminRoleCardFilter(Filter):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, callback: CallbackQuery) -> bool:
        data = callback.data or ""
        if not data.startswith("hier:role:"):
            return False
        parts = data.split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return False

        async with self.session_factory() as session:
            role_name = (
                await session.execute(
                    select(AdminRole.name).where(
                        AdminRole.id == role_id,
                        AdminRole.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
        return role_name in STANDARD_NAMES and role_name != HELPER_ROLE


def _permission_text(permissions: dict[str, bool]) -> str:
    titles = dict(KNOWN_PERMISSIONS)
    enabled = [titles[key] for key, _ in KNOWN_PERMISSIONS if permissions.get(key, False)]
    if not enabled:
        return "• нет включённых действий"
    return "\n".join(f"• {title}" for title in enabled)


def _telegram_rights_text(role_name: str) -> str:
    rights = _standard_telegram_rights(role_name) or {}
    labels = [
        ("can_manage_chat", "Управление группой"),
        ("can_delete_messages", "Удаление сообщений"),
        ("can_restrict_members", "Блокировка/ограничение участников"),
        ("can_manage_video_chats", "Управление голосовыми чатами"),
        ("can_invite_users", "Приглашение пользователей"),
        ("can_pin_messages", "Закрепление/открепление сообщений"),
        ("can_promote_members", "Назначение администраторов"),
    ]
    lines = [f"{'✅' if rights.get(key, False) else '❌'} {title}" for key, title in labels]
    return "\n".join(lines)


def create_standard_rank_cards_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="standard_rank_cards")

    @router.callback_query(StandardAdminRoleCardFilter(session_factory))
    async def standard_role_card(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return

            role = (
                await session.execute(
                    select(AdminRole).where(
                        AdminRole.id == role_id,
                        AdminRole.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
            if role is None or role.name not in STANDARD_NAMES or role.name == HELPER_ROLE:
                return

            rows = (
                await session.execute(
                    select(AdminPermission).where(AdminPermission.role_id == role_id)
                )
            ).scalars().all()
            permissions = {row.permission: bool(row.allowed) for row in rows}
            count = int((
                await session.execute(
                    select(func.count())
                    .select_from(AdminAssignment)
                    .where(AdminAssignment.role_id == role_id)
                )
            ).scalar_one())

        _, limit, position = RANK_META[role.name]
        cap = "∞" if limit is None else str(limit)
        limit_text = "без ограничений" if limit is None else str(limit)
        text = (
            "👑 <b>Стандартный админ-ранг</b>\n\n"
            f"Ранг: <b>{escape(role.name)}</b>\n"
            f"Уровень иерархии: <b>{position}/5</b>\n"
            f"Назначено: <b>{count}/{cap}</b>\n"
            f"Лимит назначений: <b>{limit_text}</b>\n\n"
            "🧩 <b>Права внутри Mimorus</b>\n"
            f"{_permission_text(permissions)}\n\n"
            "📱 <b>Telegram-права при назначении</b>\n"
            f"{_telegram_rights_text(role.name)}\n\n"
            "ℹ️ Право «Назначение администраторов» Mimorus автоматически не выдаёт, "
            "чтобы нельзя было обойти иерархию рангов через Telegram."
        )

        if callback.message is not None:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🛡 Настроить права Mimorus",
                        callback_data=f"gctl:role:{chat_id}:{role.id}",
                    )],
                    [InlineKeyboardButton(
                        text="➕ Назначить пользователя",
                        callback_data=f"hier:assign:{chat_id}:{role.id}",
                    )],
                    [InlineKeyboardButton(
                        text="📋 Назначенные",
                        callback_data=f"hier:assigned:{chat_id}:{role.id}",
                    )],
                    [InlineKeyboardButton(
                        text="◀️ Ранги администрации",
                        callback_data=f"gctl:roles:{chat_id}",
                    )],
                ]),
            )
        await callback.answer()

    return router
