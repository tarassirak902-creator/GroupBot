from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import ProcessedUpdate


class IdempotencyMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        async with self.session_factory() as session:
            async with session.begin():
                claim = await session.execute(
                    insert(ProcessedUpdate)
                    .values(update_id=event.update_id)
                    .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
                    .returning(ProcessedUpdate.update_id)
                )
                if claim.scalar_one_or_none() is None:
                    return None
        return await handler(event, data)
