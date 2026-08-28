from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.services.moderation_state import expire_timed_moderation_actions

logger = logging.getLogger(__name__)


async def moderation_lifecycle_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int = 60,
) -> None:
    while True:
        try:
            expired = await expire_timed_moderation_actions(session_factory)
            if expired:
                logger.info("Expired %s timed moderation action(s)", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Moderation lifecycle iteration failed")
        await asyncio.sleep(interval_seconds)
