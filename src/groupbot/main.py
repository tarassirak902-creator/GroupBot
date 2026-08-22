import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from groupbot.config import get_settings
from groupbot.db import create_session_factory
from groupbot.middleware.idempotency import IdempotencyMiddleware
from groupbot.routers.group_commands import create_group_commands_router
from groupbot.routers.groups import create_group_router
from groupbot.routers.private import create_private_router
from groupbot.workers.group_lifecycle import group_lifecycle_worker


async def configure_group_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="❓ Помощь"),
            BotCommand(command="guide", description="📖 Как пользоваться ботом"),
            BotCommand(command="commands", description="📋 Все команды"),
            BotCommand(command="games", description="🎮 Игры"),
            BotCommand(command="profile", description="👤 Мой профиль"),
            BotCommand(command="stats", description="📊 Моя активность"),
            BotCommand(command="rules", description="📜 Правила группы"),
            BotCommand(command="support", description="🛠 Помощь и поддержка"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot = Bot(settings.bot_token)
    session_factory = create_session_factory(settings)
    dp = Dispatcher()
    dp.update.outer_middleware(IdempotencyMiddleware(session_factory))
    dp.include_router(create_group_router(session_factory))
    dp.include_router(create_group_commands_router(session_factory))
    dp.include_router(create_private_router(session_factory, settings))

    await configure_group_commands(bot)
    lifecycle_task = asyncio.create_task(group_lifecycle_worker(bot, session_factory))
    try:
        await dp.start_polling(bot)
    finally:
        lifecycle_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
