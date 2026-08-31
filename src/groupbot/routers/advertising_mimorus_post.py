from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _advertising_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📣 Рекламный пост у Mimorus (скоро)",
                    callback_data="ads:mimorus_post",
                )
            ],
            [
                InlineKeyboardButton(text="🛒 Купить рекламу", callback_data="ads:buy"),
                InlineKeyboardButton(text="💼 Продать рекламу", callback_data="ads:sell"),
            ],
            [
                InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys"),
                InlineKeyboardButton(text="📦 Мои продажи", callback_data="ads:my_sales"),
            ],
            [InlineKeyboardButton(text="⭐ Отзывы и споры", callback_data="ads:reviews")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _mimorus_post_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


HOME_TEXT = (
    "🟣 <b>Mimorus · Реклама</b>\n\n"
    "Покупайте рекламные размещения или выставляйте свою подключённую группу как площадку.\n\n"
    "⭐ <b>Важно о расчётах:</b> цена в Stars в объявлениях сейчас фиксирует согласованные условия сделки. "
    "Mimorus пока не списывает, не удерживает и не переводит Stars между покупателем и продавцом автоматически. "
    "Бот фиксирует условия, контролирует размещение и предоставляет подтверждение сделки/спор."
)

MIMORUS_POST_TEXT = (
    "📣 <b>Рекламный пост от Mimorus — скоро</b>\n\n"
    "Это отдельный будущий формат от рекламы у владельцев групп.\n\n"
    "Планируется мастер создания поста, проверка создателем Mimorus, счёт в Telegram Stars "
    "и автоматическая публикация в активных группах по условиям размещения.\n\n"
    "Сейчас этот экран информационный: заявка не создаётся, счёт не выставляется и деньги не списываются."
)


def create_advertising_mimorus_post_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_mimorus_post")

    from groupbot.routers.advertising_reviews_nav import create_advertising_reviews_nav_router
    router.include_router(create_advertising_reviews_nav_router(session_factory))

    @router.message(F.chat.type == "private", F.text == "📢 Реклама")
    async def advertising_home_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            HOME_TEXT,
            parse_mode="HTML",
            reply_markup=_advertising_keyboard(),
        )

    @router.callback_query(F.data == "ads:home")
    async def advertising_home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text(
                HOME_TEXT,
                parse_mode="HTML",
                reply_markup=_advertising_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "ads:mimorus_post")
    async def mimorus_post(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                MIMORUS_POST_TEXT,
                parse_mode="HTML",
                reply_markup=_mimorus_post_keyboard(),
            )
        await callback.answer()

    return router
