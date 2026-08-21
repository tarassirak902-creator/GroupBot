from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Achievement, UserAchievement


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


def create_achievement_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="achievements")

    @router.message(Command("achievement_add"))
    async def achievement_add(message: Message, bot: Bot) -> None:
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Эта команда доступна только в группе.")
            return
        if message.from_user is None:
            return
        if not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Создавать достижения могут только администраторы группы.")
            return

        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 4:
            await message.answer(
                "Формат:\n"
                "/achievement_add <code> <level> <название>\n\n"
                "Пример структуры: /achievement_add lvl3 3 Третий уровень"
            )
            return

        _, code, raw_level, name = parts
        code = code.strip().lower()
        if not code or len(code) > 64:
            await message.answer("Код достижения должен быть от 1 до 64 символов.")
            return

        try:
            level = int(raw_level)
        except ValueError:
            await message.answer("Уровень должен быть целым числом.")
            return
        if level < 1:
            await message.answer("Уровень должен быть не меньше 1.")
            return

        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    insert(Achievement)
                    .values(
                        chat_id=message.chat.id,
                        code=code,
                        name=name.strip(),
                        condition_type="level_reached",
                        condition_value=level,
                        reward_xp=0,
                        reward_currency=0,
                        is_active=True,
                    )
                    .on_conflict_do_nothing(constraint="uq_achievements_chat_code")
                    .returning(Achievement.id)
                )
                created_id = result.scalar_one_or_none()

        if created_id is None:
            await message.answer("Достижение с таким code уже существует в этой группе.")
            return

        await message.answer(
            f"🏅 Достижение создано: {name.strip()}\n"
            f"Условие: достичь уровня {level}\n"
            "Награды: 0 XP / 0 валюты (пока не утверждены)."
        )

    @router.message(Command("achievements"))
    async def achievements(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Эта команда доступна только в группе.")
            return
        if message.from_user is None:
            return

        async with session_factory() as session:
            result = await session.execute(
                select(Achievement.name, UserAchievement.awarded_at)
                .join(
                    UserAchievement,
                    UserAchievement.achievement_id == Achievement.id,
                )
                .where(
                    UserAchievement.chat_id == message.chat.id,
                    UserAchievement.user_id == message.from_user.id,
                )
                .order_by(UserAchievement.awarded_at.asc())
            )
            rows = result.all()

        if not rows:
            await message.answer("🏅 Полученных достижений пока нет.")
            return

        lines = ["🏅 Твои достижения:"]
        for index, (name, _) in enumerate(rows, start=1):
            lines.append(f"{index}. {name}")
        await message.answer("\n".join(lines))

    return router
