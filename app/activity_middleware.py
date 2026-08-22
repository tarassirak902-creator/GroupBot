from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Group, GroupUser, ProcessedUpdate, User


class ActivityMiddleware(BaseMiddleware):
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

                message = event.message
                if (
                    message is not None
                    and message.chat.type in {"group", "supergroup"}
                    and message.from_user is not None
                    and not message.from_user.is_bot
                ):
                    user = message.from_user
                    chat = message.chat

                    await session.execute(
                        insert(Group)
                        .values(chat_id=chat.id, title=chat.title, is_active=True)
                        .on_conflict_do_update(
                            index_elements=[Group.chat_id],
                            set_={
                                "title": chat.title,
                                "is_active": True,
                                "updated_at": func.now(),
                            },
                        )
                    )

                    await session.execute(
                        insert(User)
                        .values(
                            user_id=user.id,
                            username=user.username,
                            first_name=user.first_name,
                            last_name=user.last_name,
                            is_bot=user.is_bot,
                        )
                        .on_conflict_do_update(
                            index_elements=[User.user_id],
                            set_={
                                "username": user.username,
                                "first_name": user.first_name,
                                "last_name": user.last_name,
                                "is_bot": user.is_bot,
                                "updated_at": func.now(),
                            },
                        )
                    )

                    await session.execute(
                        insert(GroupUser)
                        .values(
                            chat_id=chat.id,
                            user_id=user.id,
                            last_activity_at=func.now(),
                        )
                        .on_conflict_do_update(
                            constraint="uq_group_users_chat_user",
                            set_={"last_activity_at": func.now()},
                        )
                    )

        return await handler(event, data)
