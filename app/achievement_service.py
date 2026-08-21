from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Achievement, GroupUser, UserAchievement, XPConfig


async def award_level_achievements(
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    user_id: int,
    current_level: int,
) -> list[Achievement]:
    awarded: list[Achievement] = []

    async with session_factory() as session:
        async with session.begin():
            achievements_result = await session.execute(
                select(Achievement).where(
                    Achievement.chat_id == chat_id,
                    Achievement.is_active.is_(True),
                    Achievement.condition_type == "level_reached",
                    Achievement.condition_value <= current_level,
                )
            )
            eligible = list(achievements_result.scalars())
            if not eligible:
                return []

            group_user_result = await session.execute(
                select(GroupUser).where(
                    GroupUser.chat_id == chat_id,
                    GroupUser.user_id == user_id,
                )
            )
            group_user = group_user_result.scalar_one_or_none()
            if group_user is None:
                return []

            for achievement in eligible:
                claim = await session.execute(
                    insert(UserAchievement)
                    .values(
                        chat_id=chat_id,
                        user_id=user_id,
                        achievement_id=achievement.id,
                    )
                    .on_conflict_do_nothing(constraint="uq_user_achievement_once")
                    .returning(UserAchievement.id)
                )
                if claim.scalar_one_or_none() is None:
                    continue

                group_user.xp += achievement.reward_xp
                group_user.balance += achievement.reward_currency
                awarded.append(achievement)

            if awarded:
                config_result = await session.execute(
                    select(XPConfig).where(XPConfig.chat_id == chat_id)
                )
                config = config_result.scalar_one_or_none()
                if config is not None and config.level_thresholds:
                    thresholds = sorted(int(value) for value in config.level_thresholds)
                    group_user.level = 1 + sum(
                        group_user.xp >= threshold for threshold in thresholds
                    )

    return awarded
