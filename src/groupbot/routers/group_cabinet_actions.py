from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.audit import write_audit
from groupbot.ui import owned_groups_keyboard


ADD_ADMIN_RIGHTS = "manage_chat+delete_messages+restrict_members+pin_messages+invite_users"


def _add_bot_url(username: str) -> str:
    return f"https://t.me/{username}?startgroup=connect&admin={ADD_ADMIN_RIGHTS}"


def _delete_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"group:delete_confirm:{chat_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"group:open:{chat_id}")],
        ]
    )


def create_group_cabinet_actions_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_cabinet_actions")

    async def _owned_groups(user_id: int):
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Group.chat_id, Group.title, Group.status)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(GroupOwner.user_id == user_id, GroupOwner.is_current.is_(True))
                    .order_by(Group.connected_at.desc().nullslast(), Group.chat_id)
                )
            ).all()

    async def _markup(bot: Bot, user_id: int) -> tuple[list, InlineKeyboardMarkup]:
        rows = await _owned_groups(user_id)
        me = await bot.get_me()
        add_url = _add_bot_url(me.username) if me.username else None
        return rows, owned_groups_keyboard(list(rows), add_bot_url=add_url)

    @router.message(F.chat.type == "private", F.text == "👥 Мои группы")
    async def my_groups(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        rows, markup = await _markup(bot, message.from_user.id)
        if rows:
            text = "👥 <b>Мои группы</b>\n\nВыберите подключённую группу или добавьте Mimorus в новую."
        else:
            text = (
                "👥 <b>Мои группы</b>\n\n"
                "У вас пока нет подключённых групп.\n\n"
                "Нажмите <b>➕ Добавить бота в группу</b> — Telegram покажет группы, "
                "где вы можете назначать администраторов."
            )
        await message.answer(text, parse_mode="HTML", reply_markup=markup)

    @router.callback_query(F.data == "group:list")
    async def group_list(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        rows, markup = await _markup(bot, callback.from_user.id)
        text = (
            "👥 <b>Мои группы</b>\n\nВыберите подключённую группу или добавьте Mimorus в новую."
            if rows
            else "👥 <b>Мои группы</b>\n\nУ вас пока нет подключённых групп."
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()

    @router.callback_query(F.data.startswith("group:delete_prompt:"))
    async def delete_prompt(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            chat_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Group, GroupOwner)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(
                        Group.chat_id == chat_id,
                        GroupOwner.user_id == callback.from_user.id,
                        GroupOwner.is_current.is_(True),
                    )
                )
            ).first()
        if row is None:
            await callback.answer("Эта группа уже не привязана к вашему аккаунту.", show_alert=True)
            return
        group, _owner = row
        await callback.message.edit_text(
            "🗑 <b>Удалить группу из Mimorus?</b>\n\n"
            f"Группа: <b>{group.title or group.chat_id}</b>\n\n"
            "После подтверждения группа исчезнет из «Мои группы», функции Mimorus в ней отключатся, "
            "а бот выйдет из Telegram-группы. История останется в базе для аудита и повторного подключения.",
            parse_mode="HTML",
            reply_markup=_delete_confirm_keyboard(chat_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("group:delete_confirm:"))
    async def delete_confirm(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        try:
            chat_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная группа.", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        group_title: str | None = None
        async with session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(Group, GroupOwner)
                        .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                        .where(
                            Group.chat_id == chat_id,
                            GroupOwner.user_id == callback.from_user.id,
                            GroupOwner.is_current.is_(True),
                        )
                        .with_for_update()
                    )
                ).first()
                if row is None:
                    await callback.answer("Группа уже удалена из вашего кабинета.", show_alert=True)
                    return
                group, owner = row
                group_title = group.title
                owner.is_current = False
                owner.revoked_at = now
                group.status = GroupStatus.left.value
                group.disabled_at = now
                group.disconnect_deadline_at = None
                group.connect_deadline_at = None
                await write_audit(
                    session,
                    "group.removed_from_cabinet",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="group",
                    target_id=str(chat_id),
                )

        try:
            await bot.send_message(
                chat_id,
                "🗑 Группа отключена от Mimorus владельцем. Бот покидает группу.",
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat_id)
        except Exception:
            pass

        rows, markup = await _markup(bot, callback.from_user.id)
        await callback.message.edit_text(
            f"✅ Группа <b>{group_title or chat_id}</b> удалена из Mimorus.\n\n"
            + ("Выберите другую группу:" if rows else "Подключённых групп больше нет."),
            parse_mode="HTML",
            reply_markup=markup,
        )
        await callback.answer("Группа удалена")

    return router
