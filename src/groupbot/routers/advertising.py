from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.ui import private_main_menu


def _advertising_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Рекламная площадка", callback_data="ads:marketplace")],
            [InlineKeyboardButton(text="🔔 Обязательные подписки", callback_data="ads:mandatory")],
            [InlineKeyboardButton(text="⭐ Отзывы и споры", callback_data="ads:reviews")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _advertising_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _creator_advertising_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Рекламная площадка", callback_data="creator:ads:marketplace")],
            [InlineKeyboardButton(text="🔔 Обязательные подписки", callback_data="creator:ads:mandatory")],
            [InlineKeyboardButton(text="⭐ Отзывы и споры", callback_data="creator:ads:reviews")],
            [InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _creator_advertising_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Creator-реклама", callback_data="creator:ads:home")],
            [InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")],
        ]
    )


def create_advertising_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Router:
    del session_factory  # Reserved for Phase 8 persistence blocks.
    router = Router(name="advertising")

    @router.message(F.chat.type == "private", F.text == "📢 Реклама")
    async def advertising_home_message(message: Message) -> None:
        await message.answer(
            "📢 <b>Реклама</b>\n\n"
            "Раздел рекламы Mimorus. Выберите нужное направление:",
            parse_mode="HTML",
            reply_markup=_advertising_keyboard(),
        )

    @router.callback_query(F.data == "ads:home")
    async def advertising_home(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "📢 <b>Реклама</b>\n\n"
                "Раздел рекламы Mimorus. Выберите нужное направление:",
                parse_mode="HTML",
                reply_markup=_advertising_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:marketplace")
    async def advertising_marketplace(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "📣 <b>Рекламная площадка</b>\n\n"
                "Точка входа рекламного маркетплейса создана. "
                "Сделки, размещения и расчёты будут подключены следующим блоком фазы 8.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:mandatory")
    async def mandatory_subscriptions(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "🔔 <b>Обязательные подписки</b>\n\n"
                "Точка входа обязательных подписок создана. "
                "Правила проверки и управления подключим отдельным блоком фазы 8.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:reviews")
    async def advertising_reviews(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "⭐ <b>Отзывы и споры</b>\n\n"
                "Точка входа отзывов и споров создана. "
                "Отзывы, статусы споров и решения будут добавлены отдельным блоком фазы 8.",
                parse_mode="HTML",
                reply_markup=_advertising_back_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.in_({"creator:section:ads", "creator:ads:home"}))
    async def creator_advertising_home(callback: CallbackQuery) -> None:
        if callback.from_user.id not in settings.creator_id_set:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_text(
                "📢 <b>Creator-реклама</b>\n\n"
                "Управление рекламной частью Mimorus:",
                parse_mode="HTML",
                reply_markup=_creator_advertising_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("creator:ads:"))
    async def creator_advertising_section(callback: CallbackQuery) -> None:
        if callback.from_user.id not in settings.creator_id_set:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        section = (callback.data or "").rsplit(":", 1)[-1]
        labels = {
            "marketplace": "📣 Рекламная площадка",
            "mandatory": "🔔 Обязательные подписки",
            "reviews": "⭐ Отзывы и споры",
        }
        label = labels.get(section)
        if label is None:
            return
        if callback.message is not None:
            await callback.message.edit_text(
                f"{label}\n\n"
                "Creator-инструменты этого блока будут подключены вместе с соответствующей "
                "моделью и рабочей логикой, без параллельных реализаций.",
                reply_markup=_creator_advertising_back_keyboard(),
            )
        await callback.answer()

    return router
