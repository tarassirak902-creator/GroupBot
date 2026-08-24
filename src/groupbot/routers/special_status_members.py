from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupMember, MemberStatus, User
from groupbot.routers.admin_hierarchy import SPECIAL_STATUSES
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.routers.member_status_guard import is_regular_group_member
from groupbot.services.audit import write_audit


def _label(user: User) -> str:
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    if not name:
        name = f"@{user.username}" if user.username else str(user.telegram_user_id)
    return f"{name} | @{user.username}"[:64] if user.username else name[:64]


async def _regular_members(session: AsyncSession, bot: Bot, chat_id: int) -> list[User]:
    admins = {member.user.id for member in await bot.get_chat_administrators(chat_id)}
    users = list((await session.execute(
        select(User)
        .join(GroupMember, GroupMember.user_id == User.telegram_user_id)
        .where(
            GroupMember.chat_id == chat_id,
            GroupMember.status == MemberStatus.member.value,
            User.is_bot.is_(False),
        )
        .order_by(GroupMember.last_activity_at.desc().nullslast())
        .limit(100)
    )).scalars().all())
    return [user for user in users if user.telegram_user_id not in admins]


def create_special_status_members_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="special_status_members")

    @router.callback_query(F.data.startswith("hier:special_add:"))
    async def choose_member(callback: CallbackQuery, bot: Bot) -> None:
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
            members = await _regular_members(session, bot, chat_id)
        if not members:
            await callback.answer("Нет доступных обычных участников.", show_alert=True)
            return
        rows = [[InlineKeyboardButton(text=_label(user), callback_data=f"special:set:{chat_id}:{status}:{user.telegram_user_id}")] for user in members]
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"hier:special_list:{chat_id}:{status}")])
        if callback.message is not None:
            await callback.message.edit_text(
                f"{SPECIAL_STATUSES[status]} <b>— назначение</b>\n\n"
                "Показываются только обычные участники. Владелец, администраторы и боты исключены.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("special:set:"))
    async def set_status(callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or "").split(":", 4)
        try:
            chat_id = int(parts[2])
            status = parts[3]
            user_id = int(parts[4])
        except (ValueError, IndexError):
            return
        if status not in SPECIAL_STATUSES:
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
        try:
            if not await is_regular_group_member(bot, chat_id, user_id):
                await callback.answer("VIP и Недотрога назначаются только обычным участникам.", show_alert=True)
                return
        except Exception:
            await callback.answer("Не удалось перепроверить участника.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                settings = await _ensure_group_settings(session, chat_id)
                config = dict(settings.moderation_config or {})
                statuses = dict(config.get("special_statuses") or {})
                ids = [int(value) for value in statuses.get(status, [])]
                if user_id not in ids:
                    ids.append(user_id)
                statuses[status] = ids
                config["special_statuses"] = statuses
                settings.moderation_config = config
                await write_audit(session, "group.special_status_added", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="user", target_id=str(user_id), payload={"status": status})
        await callback.answer(f"✅ {SPECIAL_STATUSES[status]} назначен.")
        if callback.message is not None:
            await callback.message.edit_text(
                f"✅ {SPECIAL_STATUSES[status]} назначен.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку", callback_data=f"hier:special_list:{chat_id}:{status}")]]),
            )

    return router
