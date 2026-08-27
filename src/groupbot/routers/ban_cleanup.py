from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.manual_moderation import (
    _configured_reasons,
    _execute_action,
    _group_ready,
    _moderation_config,
)
from groupbot.routers.message_operations import cleanup_user_messages
from groupbot.services.permissions import has_permission


def _confirm_keyboard(
    chat_id: int,
    target_id: int,
    index_token: str,
    *,
    can_clean: bool,
) -> InlineKeyboardMarkup:
    action_row = [
        InlineKeyboardButton(
            text="⛔ Бан",
            callback_data=f"bclean:ban:{chat_id}:{target_id}:{index_token}",
        )
    ]
    if can_clean:
        action_row.append(
            InlineKeyboardButton(
                text="⛔🧹 Бан + очистка",
                callback_data=f"bclean:clean:{chat_id}:{target_id}:{index_token}",
            )
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            action_row,
            [InlineKeyboardButton(text="❌ Отмена", callback_data="bclean:cancel")],
        ]
    )


def create_ban_cleanup_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="ban_cleanup")

    @router.callback_query(F.data.startswith("modb:b:"))
    async def intercept_ban_reason(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            return
        try:
            chat_id = int(parts[2])
            target_id = int(parts[3])
        except ValueError:
            return
        index_token = parts[4]
        if callback.message.chat.id != chat_id:
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        async with session_factory() as session:
            if not await _group_ready(session, chat_id):
                await callback.answer("Функции группы сейчас недоступны.", show_alert=True)
                return
            if not await has_permission(session, chat_id, callback.from_user.id, "ban"):
                await callback.answer("Недостаточно прав Mimorus для бана.", show_alert=True)
                return
            can_clean = await has_permission(session, chat_id, callback.from_user.id, "delete")
            if index_token != "x":
                reasons = _configured_reasons(await _moderation_config(session, chat_id), "ban")
                try:
                    reasons[int(index_token)]
                except (ValueError, IndexError):
                    await callback.answer("Причина больше недоступна.", show_alert=True)
                    return
        description = (
            "Выберите обычный бан или бан с очисткой сохранённых сообщений пользователя."
            if can_clean
            else "У вашего ранга нет права на удаление сообщений, поэтому доступен только обычный бан."
        )
        await callback.message.edit_text(
            "⛔ <b>Подтверждение бана</b>\n\n" + description,
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(chat_id, target_id, index_token, can_clean=can_clean),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("bclean:"))
    async def confirm(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data == "bclean:cancel":
            if callback.message is not None:
                await callback.message.edit_text("❌ Действие отменено.")
            await callback.answer()
            return
        if callback.message is None:
            return
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            return
        mode = parts[1]
        try:
            chat_id = int(parts[2])
            target_id = int(parts[3])
        except ValueError:
            return
        index_token = parts[4]
        if mode not in {"ban", "clean"} or callback.message.chat.id != chat_id:
            await callback.answer("Некорректное действие.", show_alert=True)
            return

        async with session_factory() as session:
            if not await _group_ready(session, chat_id):
                await callback.answer("Функции группы сейчас недоступны.", show_alert=True)
                return
            if not await has_permission(session, chat_id, callback.from_user.id, "ban"):
                await callback.answer("Недостаточно прав Mimorus для бана.", show_alert=True)
                return
            if mode == "clean" and not await has_permission(session, chat_id, callback.from_user.id, "delete"):
                await callback.answer(
                    "Для бана с очисткой нужно право «Удаление сообщений».",
                    show_alert=True,
                )
                return
            reasons = _configured_reasons(await _moderation_config(session, chat_id), "ban")

        reason = None
        if index_token != "x":
            try:
                item = reasons[int(index_token)]
            except (ValueError, IndexError):
                await callback.answer("Причина больше недоступна.", show_alert=True)
                return
            reason = str(item.get("text") or "").strip() or None

        try:
            target_member = await bot.get_chat_member(chat_id, target_id)
            status = getattr(target_member.status, "value", str(target_member.status))
            if status == "creator":
                await callback.answer("Владельца группы нельзя наказать.", show_alert=True)
                return
            text = await _execute_action(
                bot=bot,
                session_factory=session_factory,
                chat_id=chat_id,
                actor=callback.from_user,
                target=target_member.user,
                action="ban",
                reason=reason,
            )
            if mode == "clean":
                deleted, attempted = await cleanup_user_messages(
                    bot=bot,
                    session_factory=session_factory,
                    chat_id=chat_id,
                    target_user_id=target_id,
                    actor_user_id=callback.from_user.id,
                )
                text += f"\n\n🧹 Очистка сообщений: удалено <b>{deleted}</b> из <b>{attempted}</b> сохранённых."
        except Exception as exc:
            await callback.answer(f"Не удалось выполнить действие: {str(exc)[:120]}", show_alert=True)
            return

        await callback.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        await callback.answer("Выполнено")

    return router
