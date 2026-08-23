from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.group_control import _owner_access
from groupbot.routers.user_display import clickable_identity


def create_admins_display_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="admins_display")

    @router.callback_query(F.data.startswith("gctl:admins:"))
    async def admins(callback: CallbackQuery, bot: Bot) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return

        try:
            telegram_admins = await bot.get_chat_administrators(chat_id)
        except Exception:
            await callback.answer("Не удалось получить список администраторов Telegram.", show_alert=True)
            return

        lines = [
            "👮 <b>Администраторы</b>",
            "",
            f"Telegram-администраторов: <b>{len(telegram_admins)}</b>",
            "",
        ]

        for member in telegram_admins[:30]:
            user = member.user
            role_name = "владелец" if member.status == "creator" else "администратор"

            if user.is_bot:
                # Bot accounts do not have a normal human profile target for our UI rule.
                label = user.full_name or (f"@{user.username}" if user.username else "Бот")
            else:
                label = clickable_identity(
                    telegram_user_id=user.id,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    username=user.username,
                )

            lines.append(f"• {label} — {role_name}")

        if callback.message is not None:
            await callback.message.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="◀️ Администрация",
                                callback_data=f"group:section:{chat_id}:administration",
                            )
                        ]
                    ]
                ),
            )
        await callback.answer()

    return router
