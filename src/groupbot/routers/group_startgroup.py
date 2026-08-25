from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.services.diagnostics import rights_diagnostic
from groupbot.services.groups import connect_group


_STARTGROUP_CONNECT_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?\s+connect\s*$", re.IGNORECASE)


def create_group_startgroup_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_startgroup")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(_STARTGROUP_CONNECT_RE),
    )
    async def startgroup_connect(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        try:
            async with session_factory() as session:
                async with session.begin():
                    await connect_group(session, bot, message.chat.id, message.from_user)
        except PermissionError as exc:
            error = str(exc)
            if error == "only_chat_owner":
                await message.answer(
                    "❌ Подключить группу к Mimorus может только её фактический владелец."
                )
                return
            if error == "bot_not_admin":
                await message.answer(
                    "❌ Mimorus добавлен, но не получил права администратора.\n\n"
                    "Вернитесь в личные сообщения → 👥 Мои группы → ➕ Добавить бота в группу "
                    "и подтвердите предложенные права."
                )
                return
            if error.startswith("group_limit_reached:"):
                try:
                    limit = int(error.rsplit(":", 1)[1])
                except ValueError:
                    limit = 0
                await message.answer(
                    "❌ Достигнут лимит подключённых групп текущего тарифа"
                    + (f": {limit}." if limit else ".")
                )
                return
            raise

        diagnostic, critical_ok = await rights_diagnostic(bot, message.chat.id)
        suffix = (
            "\n\n✅ Все критические права доступны."
            if critical_ok
            else "\n\n⚠️ Не хватает критических прав. Откройте «Диагностика» в кабинете группы."
        )
        await message.answer(
            "✅ <b>Группа успешно подключена к Mimorus!</b>\n\n"
            "Больше ничего вручную вводить не нужно. Настройки доступны в личных сообщениях с ботом.\n\n"
            + diagnostic
            + suffix,
            parse_mode="HTML",
        )

    return router
