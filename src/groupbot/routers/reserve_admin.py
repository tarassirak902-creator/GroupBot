from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, User
from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.audit import write_audit
from groupbot.services.users import upsert_user


async def _current_reserve(session: AsyncSession, chat_id: int):
    return (
        await session.execute(
            select(AdminAssignment, User)
            .join(User, User.telegram_user_id == AdminAssignment.user_id)
            .where(AdminAssignment.chat_id == chat_id, AdminAssignment.is_reserve.is_(True))
            .limit(1)
        )
    ).first()


async def _active_telegram_admins(bot: Bot, chat_id: int):
    """Return real active Telegram admins, excluding the owner and bots."""
    members = await bot.get_chat_administrators(chat_id)
    result = []
    for member in members:
        user = member.user
        if member.status == "creator" or user.is_bot:
            continue
        result.append(user)
    return result


def _back(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")]
    ])


async def _render(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    async with session_factory() as session:
        if not await _owner_access(session, chat_id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        current = await _current_reserve(session, chat_id)

    if current is None:
        current_text = "не назначен"
    else:
        _, user = current
        current_text = clickable_user_display(user)

    rows = [[InlineKeyboardButton(text="👤 Выбрать резервного администратора", callback_data=f"reserve:choose:{chat_id}")]]
    if current is not None:
        rows.append([InlineKeyboardButton(text="❌ Снять резервного администратора", callback_data=f"reserve:clear:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")])

    if callback.message:
        await callback.message.edit_text(
            "🧯 <b>Резервный администратор</b>\n\n"
            f"Текущий резервный администратор: {current_text}\n\n"
            "Резерв можно назначить только из действующих Telegram-администраторов группы. "
            "Владелец группы и боты в список выбора не попадают.\n\n"
            "На группу назначается один резервный администратор. При выборе нового предыдущий резерв автоматически снимается. "
            "Если у администратора уже есть ранг Mimorus, он сохраняется.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


def create_reserve_admin_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="reserve_admin")

    @router.callback_query(F.data.startswith("gctl:reserve:"))
    async def open_reserve(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("reserve:choose:"))
    async def choose(callback: CallbackQuery, bot: Bot) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            current = await _current_reserve(session, chat_id)
            current_id = current[0].user_id if current else None

        try:
            admins = await _active_telegram_admins(bot, chat_id)
        except Exception:
            await callback.answer("Не удалось получить список администраторов Telegram.", show_alert=True)
            return

        if not admins:
            await callback.answer("В группе нет других действующих администраторов.", show_alert=True)
            return

        rows: list[list[InlineKeyboardButton]] = []
        for user in admins:
            marker = "✅ " if user.id == current_id else ""
            label = user.full_name or (f"@{user.username}" if user.username else "Администратор")
            if user.username:
                label = f"{label} | @{user.username}"
            rows.append([
                InlineKeyboardButton(
                    text=f"{marker}{label}"[:64],
                    callback_data=f"reserve:set:{chat_id}:{user.id}",
                )
            ])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gctl:reserve:{chat_id}")])

        if callback.message:
            await callback.message.edit_text(
                "🧯 <b>Выбор резервного администратора</b>\n\n"
                "Выберите одного из действующих администраторов Telegram:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("reserve:set:"))
    async def set_reserve(callback: CallbackQuery, bot: Bot) -> None:
        _, _, chat_raw, user_raw = (callback.data or "").split(":", 3)
        chat_id, user_id = int(chat_raw), int(user_raw)

        try:
            telegram_admins = await bot.get_chat_administrators(chat_id)
        except Exception:
            await callback.answer("Не удалось проверить администраторов Telegram.", show_alert=True)
            return

        selected = None
        for member in telegram_admins:
            if member.user.id != user_id:
                continue
            if member.status == "creator" or member.user.is_bot:
                break
            selected = member.user
            break
        if selected is None:
            await callback.answer("Пользователь больше не является действующим администратором группы.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return

                await upsert_user(session, selected)
                await session.flush()

                await session.execute(
                    update(AdminAssignment)
                    .where(AdminAssignment.chat_id == chat_id, AdminAssignment.is_reserve.is_(True))
                    .values(is_reserve=False)
                )
                assignment = (
                    await session.execute(
                        select(AdminAssignment)
                        .where(AdminAssignment.chat_id == chat_id, AdminAssignment.user_id == user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if assignment is None:
                    assignment = AdminAssignment(chat_id=chat_id, user_id=user_id, role_id=None, is_reserve=True)
                    session.add(assignment)
                else:
                    assignment.is_reserve = True

                await write_audit(
                    session,
                    "group.reserve_admin_set",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="user",
                    target_id=str(user_id),
                    payload={"telegram_admin_verified": True},
                )
        await _render(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("reserve:clear:"))
    async def clear_reserve(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                current = await _current_reserve(session, chat_id)
                if current is None:
                    await callback.answer("Резервный администратор не назначен.", show_alert=True)
                    return
                assignment, _ = current
                assignment.is_reserve = False
                await write_audit(
                    session,
                    "group.reserve_admin_cleared",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="user",
                    target_id=str(assignment.user_id),
                    payload={},
                )
        await _render(callback, session_factory, chat_id)

    return router
