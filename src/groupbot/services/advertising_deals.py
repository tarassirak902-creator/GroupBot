from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.advertising_models import (
    AdvertisingDeal,
    AdvertisingDispute,
    AdvertisingNoClaimsConfirmation,
)
from groupbot.services.audit import write_audit

NO_CLAIMS_TIMEOUT = timedelta(hours=5)
FINAL_STATUSES = {"completed_mutual", "completed_timeout", "completed_after_dispute"}


async def mark_no_claims(
    session: AsyncSession,
    deal_id: int,
    user_id: int,
) -> tuple[AdvertisingDeal | None, str]:
    deal = (
        await session.execute(
            select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
        )
    ).scalar_one_or_none()
    if deal is None:
        return None, "not_found"
    if user_id not in {deal.seller_user_id, deal.buyer_user_id}:
        return deal, "forbidden"
    if deal.status in FINAL_STATUSES:
        return deal, "already_completed"
    if deal.status == "dispute_open":
        return deal, "dispute_open"
    if deal.status != "finished_waiting_confirmation":
        return deal, "not_finished"

    existing = (
        await session.execute(
            select(AdvertisingNoClaimsConfirmation.id).where(
                AdvertisingNoClaimsConfirmation.deal_id == deal.id,
                AdvertisingNoClaimsConfirmation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return deal, "already_confirmed"

    now = datetime.now(timezone.utc)
    session.add(
        AdvertisingNoClaimsConfirmation(
            deal_id=deal.id,
            user_id=user_id,
            confirmed_at=now,
        )
    )
    await session.flush()

    confirmations = (
        await session.execute(
            select(func.count())
            .select_from(AdvertisingNoClaimsConfirmation)
            .where(AdvertisingNoClaimsConfirmation.deal_id == deal.id)
        )
    ).scalar_one()

    if confirmations >= 2:
        deal.status = "completed_mutual"
        deal.completed_at = now
        deal.no_claims_deadline_at = None
        result = "completed_mutual"
    else:
        deal.first_no_claims_at = now
        deal.no_claims_deadline_at = now + NO_CLAIMS_TIMEOUT
        result = "waiting_other_party"

    await write_audit(
        session,
        "advertising.no_claims_confirmed",
        actor_user_id=user_id,
        target_type="advertising_deal",
        target_id=str(deal.id),
        payload={
            "result": result,
            "deadline_at": deal.no_claims_deadline_at.isoformat()
            if deal.no_claims_deadline_at
            else None,
        },
    )
    return deal, result


async def open_dispute(
    session: AsyncSession,
    deal_id: int,
    user_id: int,
    *,
    reason: str | None = None,
    description: str | None = None,
) -> tuple[AdvertisingDispute | None, str]:
    deal = (
        await session.execute(
            select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
        )
    ).scalar_one_or_none()
    if deal is None:
        return None, "not_found"
    if user_id not in {deal.seller_user_id, deal.buyer_user_id}:
        return None, "forbidden"
    if deal.status in FINAL_STATUSES:
        return None, "deal_closed"
    if deal.status not in {"active", "finished_waiting_confirmation", "dispute_open"}:
        return None, "not_available"

    existing = (
        await session.execute(
            select(AdvertisingDispute).where(AdvertisingDispute.deal_id == deal.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, "already_open"

    dispute = AdvertisingDispute(
        deal_id=deal.id,
        opened_by_user_id=user_id,
        status="open",
        reason=reason,
        description=description,
    )
    session.add(dispute)
    deal.status = "dispute_open"
    deal.no_claims_deadline_at = None
    await session.flush()
    await write_audit(
        session,
        "advertising.dispute_opened",
        actor_user_id=user_id,
        target_type="advertising_deal",
        target_id=str(deal.id),
        payload={"dispute_id": dispute.id, "reason": reason},
    )
    return dispute, "opened"
