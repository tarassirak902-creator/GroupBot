from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualOp
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_owner

logger = logging.getLogger(__name__)


async def run_advertising_manual_lifecycle_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Finish expired day campaigns and stop manual OP whose source is unavailable.

    Subscriber-target campaigns intentionally remain active at the target. Their
    progress represents the current live subscriber count and can fall again after
    a voluntary leave (for example 10/10 -> 9/10 -> 10/10). They therefore finish
    only when the owner stops them or the source group becomes unavailable.
    """
    now = datetime.now(timezone.utc)
    changed = 0

    async with session_factory() as session:
        async with session.begin():
            ops = list((await session.execute(
                select(AdvertisingManualOp)
                .where(AdvertisingManualOp.status == "active")
                .order_by(AdvertisingManualOp.id)
                .with_for_update(skip_locked=True)
            )).scalars().all())

            for op in ops:
                if op.mode == "days" and op.ends_at is not None and op.ends_at <= now:
                    op.status = "completed"
                    op.completed_at = now
                    changed += 1
                    continue

                group_status = (await session.execute(
                    select(Group.status).where(Group.chat_id == op.source_chat_id)
                )).scalar_one_or_none()
                current_owner = (await session.execute(
                    select(GroupOwner.user_id).where(
                        GroupOwner.chat_id == op.source_chat_id,
                        GroupOwner.is_current.is_(True),
                    )
                )).scalar_one_or_none()

                source_available = (
                    group_status == GroupStatus.active.value
                    and current_owner == op.owner_user_id
                )
                if source_available:
                    source_available = (
                        await active_subscription_for_owner(session, op.owner_user_id)
                    ) is not None

                if not source_available:
                    op.status = "stopped"
                    op.completed_at = now
                    changed += 1

    return changed


async def advertising_manual_lifecycle_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int = 30,
) -> None:
    while True:
        try:
            await run_advertising_manual_lifecycle_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Manual advertising lifecycle iteration failed")
        await asyncio.sleep(interval_seconds)
