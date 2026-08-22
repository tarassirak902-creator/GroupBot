from aiogram.types import User as TelegramUser
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import User


async def upsert_user(session: AsyncSession, user: TelegramUser) -> None:
    await session.execute(
        insert(User)
        .values(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            is_bot=user.is_bot,
            is_premium=bool(user.is_premium),
            deleted_account=False,
        )
        .on_conflict_do_update(
            index_elements=[User.telegram_user_id],
            set_={
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_bot": user.is_bot,
                "is_premium": bool(user.is_premium),
                "deleted_account": False,
                "updated_at": func.now(),
            },
        )
    )
