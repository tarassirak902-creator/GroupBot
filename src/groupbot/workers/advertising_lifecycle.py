from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal
from groupbot.services.audit import write_audit

logger = logging.getLogger(__name__)


async def close_expired_no_claims_windows(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    now = datetime.now(timezone.utc)
    closed = 0
    async with session_factory() as session:
        async with session.begin():
            deals = (
                await session.execute(
                    select(AdvertisingDeal)
                    .where(
                        AdvertisingDeal.status == "finished_waiting_confirmation",
                        AdvertisingDeal.first_no_claims_at.is_not(None),
                        AdvertisingDeal.no_claims_deadline_at.is_not(None),
                        AdvertisingDeal.no_claims_deadline_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for deal in deals:
                deal.status = "completed_timeout"
                deal.completed_at = now
                await write_audit(
                    session,
                    "advertising.deal_completed_timeout",
                    actor_user_id=None,
                    target_type="advertising_deal",
                    target_id=str(deal.id),
                    payload={
                        "seller_user_id": deal.seller_user_id,
                        "buyer_user_id": deal.buyer_user_id,
                        "first_no_claims_at": deal.first_no_claims_at.isoformat()
                        if deal.first_no_claims_at
                        else None,
                        "deadline_at": deal.no_claims_deadline_at.isoformat()
                        if deal.no_claims_deadline_at
                        else None,
                    },
                )
                closed += 1
    return closed


async def advertising_lifecycle_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    while True:
        try:
            closed = await close_expired_no_claims_windows(session_factory)
            if closed:
                logger.info("Closed %s advertising deals after no-claims timeout", closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Advertising lifecycle iteration failed")
        await asyncio.sleep(60)
