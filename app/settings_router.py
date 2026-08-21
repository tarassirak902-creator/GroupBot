from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GroupSettings

SETTING_LABELS = {
    "rp_enabled": "RP",
    "xp_enabled": "XP и уровни",
    "economy_enabled": "Экономика",
    "auto_activity_enabled": "Автоактивности",
    "moderation_enabled": "Модерация",
}


def _keyboard(settings: GroupSettings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field, label in SETTING_LABELS.items():
        enabled = bool(getattr(settings, field))
        icon = "✅" if enabled else "❌"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {label}",
                    callback_data=f"settings:toggle:{field}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _text() -> str:
    return (
        "⚙️ Настройки GroupBot для этой группы\n\n"
        "Нажмите кнопку, чтобы включить или отключить модуль.\n"
        "Настройки действуют только в текущей группе."
    )


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


async def _get_or_create_settings(
    session: AsyncSession,
    chat_id: int,
) -> GroupSettings:
    await session.execute(
        insert(GroupSettings)
        .values(chat_id=chat_id)
        .on_conflict_do_nothing(index_elements=[GroupSettings.chat_id])
    )
    await session.flush()
    result = await session.execute(
        select(GroupSettings).where(GroupSettings.chat_id == chat_id)
    )
    return result.scalar_one()


def create_settings_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_settings")

    @router.message(Command("settings"))
    async def settings_handler(message: Message, bot: Bot) -> None:
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Эта команда доступна только в группе.")
            return
        if message.from_user is None:
            return
        if not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Изменять настройки могут только администраторы группы.")
            return

        async with session_factory() as session:
            async with session.begin():
                settings = await _get_or_create_settings(session, message.chat.id)
                keyboard = _keyboard(settings)
        await message.answer(_text(), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("settings:toggle:"))
    async def settings_toggle(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None or callback.from_user is None:
            await callback.answer()
            return
        chat = callback.message.chat
        if chat.type not in {"group", "supergroup"}:
            await callback.answer("Настройки доступны только в группе.", show_alert=True)
            return
        if not await _is_admin(bot, chat.id, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        field = (callback.data or "").removeprefix("settings:toggle:")
        if field not in SETTING_LABELS:
            await callback.answer("Неизвестная настройка.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                settings = await _get_or_create_settings(session, chat.id)
                setattr(settings, field, not bool(getattr(settings, field)))
                await session.flush()
                keyboard = _keyboard(settings)

        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer("Настройка обновлена")

    return router
