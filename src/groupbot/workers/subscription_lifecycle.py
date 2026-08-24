import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Subscription, SubscriptionStatus
from groupbot.services.audit import write_audit

logger = logging.getLogger(__name__)


async def expire_due_subscriptions(session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    expired_count = 0
    async with session_factory() as session:
        due_ids = (
            await session.execute(
                select(Subscription.id).where(
                    Subscription.status == SubscriptionStatus.active.value,
                    Subscription.ends_at <= now,
                )
            )
        ).scalars().all()

    for subscription_id in due_ids:
        async with session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(Subscription)
                        .where(Subscription.id == subscription_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                current = datetime.now(timezone.utc)
                if (
                    row is None
                    or row.status != SubscriptionStatus.active.value
                    or row.ends_at > current
                ):
                    continue
                row.status = SubscriptionStatus.expired.value
                await write_audit(
                    session,
                    "subscription.expired",
                    actor_user_id=row.owner_user_id,
                    target_type="subscription",
                    target_id=str(row.id),
                    payload={
                        "tariff_id": row.tariff_id,
                        "ended_at": row.ends_at.isoformat(),
                    },
                )
                expired_count += 1
    return expired_count


async def subscription_lifecycle_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    while True:
        try:
            expired_count = await expire_due_subscriptions(session_factory)
            if expired_count:
                logger.info("Marked %s subscription(s) as expired", expired_count)
        except Exception:
            logger.exception("Subscription lifecycle worker iteration failed")
        await asyncio.sleep(60)
