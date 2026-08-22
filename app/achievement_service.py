from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Achievement, GroupUser, Transaction, UserAchievement, Wallet, XPConfig


async def award_level_achievements(
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    user_id: int,
    current_level: int,
) -> list[Achievement]:
    """Award active level achievements exactly once and atomically."""
    awarded: list[Achievement] = []

    async with session_factory() as session:
        async with session.begin():
            eligible = list((await session.execute(
                select(Achievement).where(
                    Achievement.chat_id == chat_id,
                    Achievement.is_active.is_(True),
                    Achievement.condition_type == "level_reached",
                    Achievement.condition_value <= current_level,
                ).order_by(Achievement.condition_value, Achievement.id)
            )).scalars())
            if not eligible:
                return []

            group_user = (await session.execute(
                select(GroupUser).where(
                    GroupUser.chat_id == chat_id,
                    GroupUser.user_id == user_id,
                ).with_for_update()
            )).scalar_one_or_none()
            if group_user is None:
                return []

            await session.execute(
                insert(Wallet).values(
                    chat_id=chat_id,
                    user_id=user_id,
                    balance=group_user.balance,
                ).on_conflict_do_nothing(constraint="uq_wallet_chat_user")
            )
            wallet = (await session.execute(
                select(Wallet).where(
                    Wallet.chat_id == chat_id,
                    Wallet.user_id == user_id,
                ).with_for_update()
            )).scalar_one()

            for achievement in eligible:
                claim = await session.execute(
                    insert(UserAchievement).values(
                        chat_id=chat_id,
                        user_id=user_id,
                        achievement_id=achievement.id,
                    ).on_conflict_do_nothing(
                        constraint="uq_user_achievement_once"
                    ).returning(UserAchievement.id)
                )
                claim_id = claim.scalar_one_or_none()
                if claim_id is None:
                    continue

                group_user.xp += achievement.reward_xp
                if achievement.reward_currency:
                    wallet.balance += achievement.reward_currency
                    group_user.balance = wallet.balance  # transitional compatibility mirror
                    session.add(Transaction(
                        chat_id=chat_id,
                        from_user_id=None,
                        to_user_id=user_id,
                        amount=achievement.reward_currency,
                        kind="achievement_reward",
                        reference=f"achievement:{achievement.id}:claim:{claim_id}",
                    ))
                awarded.append(achievement)

            if awarded:
                config = (await session.execute(
                    select(XPConfig).where(XPConfig.chat_id == chat_id)
                )).scalar_one_or_none()
                if config is not None and config.level_thresholds:
                    thresholds = sorted(int(value) for value in config.level_thresholds)
                    group_user.level = 1 + sum(group_user.xp >= threshold for threshold in thresholds)

    return awarded
