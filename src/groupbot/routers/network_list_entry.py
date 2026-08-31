from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.routers.tariff_limits import _render_network_list


def create_network_list_entry_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="network_list_entry")

    @router.message(F.chat.type == "private", F.text == "🌐 Сетки групп")
    async def networks_menu(message: Message) -> None:
        if message.from_user is None:
            return
        sent = await message.answer("🌐 Сетки групп")
        rendered = await _render_network_list(sent, session_factory, message.from_user.id)
        if not rendered:
            await sent.edit_text("🌐 <b>Сетки групп</b>\n\nДля управления сетками нужен активный тариф.", parse_mode="HTML")

    return router