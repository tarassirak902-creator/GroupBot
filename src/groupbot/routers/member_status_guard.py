from __future__ import annotations

from aiogram import Bot


async def is_regular_group_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    if member.user.is_bot:
        return False
    return member.status not in {"creator", "administrator", "left", "kicked"}
