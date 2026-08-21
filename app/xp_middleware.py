from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.achievement_service import award_level_achievements
from app.content import render_achievement, render_level_up
from app.models import GroupSettings, GroupUser, XPConfig


class XPMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        if event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)
        if event.from_user is None or event.from_user.is_bot:
            return await handler(event, data)
        if event.text and event.text.startswith("/"):
            return await handler(event, data)

        level_up_to: int | None = None
        current_level: int | None = None

        async with self.session_factory() as session:
            async with session.begin():
                settings_result = await session.execute(
                    select(GroupSettings).where(GroupSettings.chat_id == event.chat.id)
                )
                settings = settings_result.scalar_one_or_none()
                if settings is None or not settings.xp_enabled:
                    return await handler(event, data)

                config_result = await session.execute(
                    select(XPConfig).where(XPConfig.chat_id == event.chat.id)
                )
                config = config_result.scalar_one_or_none()
                if (
                    config is None
                    or config.xp_per_message is None
                    or config.xp_per_message <= 0
                    or not config.level_thresholds
                ):
                    return await handler(event, data)

                user_result = await session.execute(
                    select(GroupUser).where(
                        GroupUser.chat_id == event.chat.id,
                        GroupUser.user_id == event.from_user.id,
                    )
                )
                group_user = user_result.scalar_one_or_none()
                if group_user is None:
                    return await handler(event, data)

                previous_level = group_user.level
                group_user.xp += config.xp_per_message
                thresholds = sorted(int(value) for value in config.level_thresholds)
                new_level = 1 + sum(group_user.xp >= threshold for threshold in thresholds)
                group_user.level = new_level
                current_level = new_level
                if new_level > previous_level:
                    level_up_to = new_level

        awarded = []
        if current_level is not None:
            awarded = await award_level_achievements(
                self.session_factory,
                event.chat.id,
                event.from_user.id,
                current_level,
            )

        result = await handler(event, data)

        if level_up_to is not None:
            await event.answer(render_level_up(event.from_user.full_name, level_up_to))

        for achievement in awarded:
            await event.answer(
                render_achievement(event.from_user.full_name, achievement.name)
            )

        return result
