from __future__ import annotations

from aiogram import Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminRole
from groupbot.routers.standard_rank_cards import create_standard_rank_cards_router
from groupbot.services.admin_rank_access import can_open_rank_management
from groupbot.services.helper_role_policy import HELPER_ROLE


class HelperAssignFilter(Filter):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, callback: CallbackQuery) -> bool:
        data = callback.data or ""
        if not data.startswith("hier:assign:"):
            return False
        parts = data.split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return False

        async with self.session_factory() as session:
            role_name = (
                await session.execute(
                    select(AdminRole.name).where(
                        AdminRole.id == role_id,
                        AdminRole.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
        return role_name == HELPER_ROLE


def create_helper_private_assignment_hint_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="helper_private_assignment_hint")

    # Standard rank cards must run before the legacy hierarchy router so owners
    # see the real Mimorus + Telegram rights matrix approved for each rank.
    router.include_router(create_standard_rank_cards_router(session_factory))

    @router.callback_query(HelperAssignFilter(session_factory))
    async def helper_assign_hint(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            role_id = int(parts[3])
        except (ValueError, IndexError):
            return

        async with session_factory() as session:
            if not await can_open_rank_management(
                session,
                chat_id=chat_id,
                actor_id=callback.from_user.id,
            ):
                await callback.answer(
                    "Ваш ранг не позволяет назначать Помощников или группа сейчас недоступна.",
                    show_alert=True,
                )
                return

        if callback.message is not None:
            await callback.message.edit_text(
                "🔹 <b>Как назначить Помощника</b>\n\n"
                "Назначение выполняется прямо в группе одним из двух способов:\n\n"
                "1️⃣ Ответьте на сообщение пользователя и напишите:\n"
                "<code>назначить помощника</code>\n\n"
                "2️⃣ Или отправьте команду:\n"
                "<code>назначить помощника @username</code>\n"
                "или\n"
                "<code>назначить помощника 123456789</code>\n\n"
                "Пользователь сразу будет закреплён за вами как за наставником. "
                "Telegram-админка Помощнику не выдаётся.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="📋 Назначенные Помощники",
                        callback_data=f"hier:assigned:{chat_id}:{role_id}",
                    )],
                    [InlineKeyboardButton(
                        text="◀️ К Помощнику",
                        callback_data=f"hier:role:{chat_id}:{role_id}",
                    )],
                ]),
            )
        await callback.answer()

    return router
