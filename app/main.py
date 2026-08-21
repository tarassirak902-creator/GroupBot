import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.achievement_router import create_achievement_router
from app.activity_middleware import ActivityMiddleware
from app.auto_activity import auto_activity_worker, create_auto_activity_router
from app.config import get_settings
from app.db import check_database, create_engine, create_session_factory
from app.economy_router import create_economy_router
from app.relationship_router import create_relationship_router
from app.rp_router import create_rp_router
from app.settings_router import create_settings_router
from app.xp_middleware import XPMiddleware
from app.xp_router import create_xp_router


dp = Dispatcher()


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer("GroupBot запущен. Базовый каркас v0.1 работает.")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = create_engine(settings.database_url)
    await check_database(engine)
    logging.getLogger(__name__).info("Database connection is ready")
    session_factory = create_session_factory(engine)
    dp.update.outer_middleware(ActivityMiddleware(session_factory))
    dp.message.outer_middleware(XPMiddleware(session_factory))
    dp.include_router(create_settings_router(session_factory))
    dp.include_router(create_xp_router(session_factory))
    dp.include_router(create_achievement_router(session_factory))
    dp.include_router(create_economy_router(session_factory))
    dp.include_router(create_rp_router(session_factory))
    dp.include_router(create_relationship_router(session_factory))
    dp.include_router(create_auto_activity_router(session_factory))

    bot = Bot(token=settings.bot_token.get_secret_value())
    worker = asyncio.create_task(auto_activity_worker(bot, session_factory))
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
