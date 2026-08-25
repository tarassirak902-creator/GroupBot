from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.advertising import _advertising_keyboard
from groupbot.routers.group_control import _owner_access


HANDLED_SECTIONS = {"automation", "games", "advertising", "settings"}


def _back_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
    ])


def create_group_sections_nav_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_sections_nav")

    @router.callback_query(
        F.data.regexp(r"^group:section:-?\d+:(automation|games|advertising|settings)$")
    )
    async def section(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректный раздел.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        section_key = parts[3]
        if section_key not in HANDLED_SECTIONS:
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return

        if callback.message is None:
            await callback.answer()
            return

        if section_key == "advertising":
            # Do not introduce a second advertising UX here. The group-management
            # button opens the same advertising home that was approved for the
            # private main menu, so every entry point stays consistent.
            await callback.message.edit_text(
                "🟣 <b>Mimorus · Реклама</b>\n\n"
                "Покупайте рекламные размещения или выставляйте свою подключённую группу как площадку.",
                parse_mode="HTML",
                reply_markup=_advertising_keyboard(),
            )
        elif section_key == "automation":
            await callback.message.edit_text(
                "🤖 <b>Автоматизация</b>\n\n"
                "Этот экран зарезервирован для автоматических сообщений, повторов, напоминаний и других сценариев группы.\n\n"
                "Функции, которые ещё не подключены, не будут имитироваться или менять настройки скрытно.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(chat_id),
            )
        elif section_key == "games":
            await callback.message.edit_text(
                "🎮 <b>Настройки развлечений</b>\n\n"
                "Здесь будут настройки игр, RP, отношений, заданий и рейтингов этой группы.\n\n"
                "Игровой блок ещё не подключён к текущему этапу разработки.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(chat_id),
            )
        else:
            await callback.message.edit_text(
                "⚙️ <b>Настройки группы</b>\n\n"
                "Основные настройки сейчас распределены по рабочим разделам: модерация, администрация, статистика и диагностика.\n\n"
                "Дополнительные общие настройки группы будут добавляться сюда по мере реализации.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(chat_id),
            )
        await callback.answer()

    return router
