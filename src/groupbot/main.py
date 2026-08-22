import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommandScopeAllGroupChats

from groupbot.config import get_settings
from groupbot.db import create_session_factory
from groupbot.middleware.idempotency import IdempotencyMiddleware
from groupbot.routers.creator import create_creator_router
from groupbot.routers.creator_subscription_duration import create_creator_subscription_duration_router
from groupbot.routers.group_commands import create_group_commands_router
from groupbot.routers.groups import create_group_router
from groupbot.routers.private import create_private_router
from groupbot.workers.group_lifecycle import group_lifecycle_worker


async def clear_global_group_commands(bot: Bot) -> None:
    # Commands are registered per chat only after an owner activates a tariff.
    # This also clears any global group command menu left from an older build.
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())


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
    # Duration presets intercept creator subscription assignment before the
    # generic creator handler, so paid tariffs offer fast 7/15/30-day choices.
    dp.include_router(create_creator_subscription_duration_router(session_factory, settings))
    # Creator router goes before the generic private router so the creator-only
    # menu button is handled by the real global panel rather than a placeholder.
    dp.include_router(create_creator_router(session_factory, settings))
    dp.include_router(create_private_router(session_factory, settings))

    await clear_global_group_commands(bot)
    lifecycle_task = asyncio.create_task(group_lifecycle_worker(bot, session_factory))
    try:
        await dp.start_polling(bot)
    finally:
        lifecycle_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
