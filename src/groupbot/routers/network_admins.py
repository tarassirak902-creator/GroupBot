from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupOwner, GroupStatus, NetworkAdmin, User
from groupbot.routers.group_control import KNOWN_PERMISSIONS, _owner_access
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.users import upsert_user


async def _owner_id(session: AsyncSession, chat_id: int) -> int | None:
    return (await session.execute(
        select(GroupOwner.user_id).where(
            GroupOwner.chat_id == chat_id,
            GroupOwner.is_current.is_(True),
        ).limit(1)
    )).scalar_one_or_none()


async def _network_groups_count(session: AsyncSession, owner_id: int) -> int:
    return int((await session.execute(
        select(func.count()).select_from(GroupOwner).join(Group, Group.chat_id == GroupOwner.chat_id).where(
            GroupOwner.user_id == owner_id,
            GroupOwner.is_current.is_(True),
            Group.status == GroupStatus.active.value,
        )
    )).scalar_one())


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        owner_id = await _owner_id(session, chat_id)
        if owner_id is None:
            await callback.answer("Не удалось определить владельца группы.", show_alert=True)
            return
        groups_count = await _network_groups_count(session, owner_id)
        rows = list((await session.execute(
            select(NetworkAdmin, User)
            .join(User, User.telegram_user_id == NetworkAdmin.user_id)
            .where(NetworkAdmin.owner_user_id == owner_id, NetworkAdmin.is_active.is_(True))
            .order_by(NetworkAdmin.created_at.asc())
        )).all())

    text_lines = [
        "🌐 <b>Сетевые администраторы</b>",
        "",
        f"Групп в сетке владельца: <b>{groups_count}</b>",
        f"Сетевых администраторов: <b>{len(rows)}</b>",
        "",
        "Сетевые права действуют только в группах, где текущим владельцем является этот же пользователь. На чужие группы права не распространяются.",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    if rows:
        text_lines.extend(["", "Назначенные:"])
        for row, user in rows:
            text_lines.append(f"• {clickable_user_display(user)}")
            label = user.first_name or user.username or str(user.telegram_user_id)
            if user.last_name:
                label = f"{label} {user.last_name}"
            buttons.append([InlineKeyboardButton(text=f"👤 {label}"[:64], callback_data=f"network:card:{chat_id}:{user.telegram_user_id}")])
    else:
        text_lines.extend(["", "Назначений пока нет."])

    buttons.append([InlineKeyboardButton(text="➕ Добавить сетевого администратора", callback_data=f"network:add:{chat_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")])
    if callback.message:
        await callback.message.edit_text(
            "\n".join(text_lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    await callback.answer()


async def _render_card(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int, user_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        owner_id = await _owner_id(session, chat_id)
        if owner_id is None:
            return
        row = (await session.execute(select(NetworkAdmin).where(
            NetworkAdmin.owner_user_id == owner_id,
            NetworkAdmin.user_id == user_id,
            NetworkAdmin.is_active.is_(True),
        ))).scalar_one_or_none()
        user = (await session.execute(select(User).where(User.telegram_user_id == user_id))).scalar_one_or_none()
        groups_count = await _network_groups_count(session, owner_id)
    if row is None or user is None:
        await callback.answer("Сетевой администратор не найден.", show_alert=True)
        return

    allowed = {str(value) for value in (row.permissions_json or [])}
    buttons = [[InlineKeyboardButton(
        text=f"{'✅' if key in allowed else '❌'} {label}",
        callback_data=f"network:perm:{chat_id}:{user_id}:{key}",
    )] for key, label in KNOWN_PERMISSIONS]
    buttons.append([InlineKeyboardButton(text="❌ Снять сетевого администратора", callback_data=f"network:remove:{chat_id}:{user_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Сетевые администраторы", callback_data=f"gctl:network_admins:{chat_id}")])
    if callback.message:
        await callback.message.edit_text(
            "🌐 <b>Сетевой администратор</b>\n\n"
            f"Пользователь: {clickable_user_display(user)}\n"
            f"Групп в сетке: <b>{groups_count}</b>\n\n"
            "Права применяются ко всем группам этого владельца. Включите только необходимые действия:",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    await callback.answer()


def create_network_admins_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="network_admins")

    @router.callback_query(F.data.startswith("gctl:network_admins:"))
    async def open_network(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("network:add:"))
    async def add_screen(callback: CallbackQuery, bot: Bot) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            owner_id = await _owner_id(session, chat_id)
            if owner_id is None:
                return
            existing = set((await session.execute(select(NetworkAdmin.user_id).where(
                NetworkAdmin.owner_user_id == owner_id,
                NetworkAdmin.is_active.is_(True),
            ))).scalars().all())

        try:
            admins = await bot.get_chat_administrators(chat_id)
        except Exception:
            await callback.answer("Не удалось получить администраторов Telegram.", show_alert=True)
            return

        candidates = [member.user for member in admins if member.status != "creator" and not member.user.is_bot and member.user.id not in existing]
        if not candidates:
            await callback.answer("Нет доступных действующих администраторов для назначения.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                for user in candidates:
                    await upsert_user(session, user)

        buttons = []
        for user in candidates:
            label = user.full_name or (f"@{user.username}" if user.username else str(user.id))
            if user.username:
                label = f"{label} | @{user.username}"
            buttons.append([InlineKeyboardButton(text=label[:64], callback_data=f"network:set:{chat_id}:{user.id}")])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:network_admins:{chat_id}")])
        if callback.message:
            await callback.message.edit_text(
                "🌐 <b>Добавить сетевого администратора</b>\n\n"
                "Выберите действующего администратора Telegram текущей группы. После назначения настройте его сетевые права:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("network:set:"))
    async def set_network(callback: CallbackQuery, bot: Bot) -> None:
        _, _, chat_raw, user_raw = (callback.data or "").split(":", 3)
        chat_id, user_id = int(chat_raw), int(user_raw)
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status not in {"administrator", "creator"} or member.status == "creator" or member.user.is_bot:
                await callback.answer("Пользователь больше не является действующим администратором.", show_alert=True)
                return
        except Exception:
            await callback.answer("Не удалось проверить администратора Telegram.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                owner_id = await _owner_id(session, chat_id)
                if owner_id is None or user_id == owner_id:
                    await callback.answer("Владельца нельзя назначить сетевым администратором.", show_alert=True)
                    return
                await upsert_user(session, member.user)
                row = (await session.execute(select(NetworkAdmin).where(
                    NetworkAdmin.owner_user_id == owner_id,
                    NetworkAdmin.user_id == user_id,
                ).with_for_update())).scalar_one_or_none()
                if row is None:
                    row = NetworkAdmin(owner_user_id=owner_id, user_id=user_id, permissions_json=[], is_active=True)
                    session.add(row)
                else:
                    row.is_active = True
                    if row.permissions_json is None:
                        row.permissions_json = []
                await write_audit(session, "network_admin.added", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="user", target_id=str(user_id), payload={"owner_user_id": owner_id})
        await _render_card(callback, session_factory, chat_id, user_id)

    @router.callback_query(F.data.startswith("network:card:"))
    async def card(callback: CallbackQuery) -> None:
        _, _, chat_raw, user_raw = (callback.data or "").split(":", 3)
        await _render_card(callback, session_factory, int(chat_raw), int(user_raw))

    @router.callback_query(F.data.startswith("network:perm:"))
    async def toggle_permission(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        chat_id, user_id, permission = int(parts[2]), int(parts[3]), parts[4]
        valid = {key for key, _ in KNOWN_PERMISSIONS}
        if permission not in valid:
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                owner_id = await _owner_id(session, chat_id)
                row = (await session.execute(select(NetworkAdmin).where(
                    NetworkAdmin.owner_user_id == owner_id,
                    NetworkAdmin.user_id == user_id,
                    NetworkAdmin.is_active.is_(True),
                ).with_for_update())).scalar_one_or_none()
                if row is None:
                    await callback.answer("Сетевой администратор не найден.", show_alert=True)
                    return
                permissions = {str(value) for value in (row.permissions_json or [])}
                if permission in permissions:
                    permissions.remove(permission)
                    allowed = False
                else:
                    permissions.add(permission)
                    allowed = True
                row.permissions_json = sorted(permissions)
                await write_audit(session, "network_admin.permission_changed", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="user", target_id=str(user_id), payload={"permission": permission, "allowed": allowed})
        await _render_card(callback, session_factory, chat_id, user_id)

    @router.callback_query(F.data.startswith("network:remove:"))
    async def remove(callback: CallbackQuery) -> None:
        _, _, chat_raw, user_raw = (callback.data or "").split(":", 3)
        chat_id, user_id = int(chat_raw), int(user_raw)
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    return
                owner_id = await _owner_id(session, chat_id)
                row = (await session.execute(select(NetworkAdmin).where(
                    NetworkAdmin.owner_user_id == owner_id,
                    NetworkAdmin.user_id == user_id,
                    NetworkAdmin.is_active.is_(True),
                ).with_for_update())).scalar_one_or_none()
                if row is None:
                    await callback.answer("Сетевой администратор уже снят.", show_alert=True)
                    return
                row.is_active = False
                await write_audit(session, "network_admin.removed", chat_id=chat_id, actor_user_id=callback.from_user.id, target_type="user", target_id=str(user_id), payload={"owner_user_id": owner_id})
        await _render(callback, session_factory, chat_id)

    return router
