from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.addon_models import SubscriptionAddon
from groupbot.models import GroupOwner, Subscription, SubscriptionStatus, Tariff
from groupbot.services.audit import write_audit


async def active_subscription_for_owner(session: AsyncSession, owner_user_id: int) -> Subscription | None:
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.owner_user_id == owner_user_id,
                Subscription.status == SubscriptionStatus.active.value,
                Subscription.ends_at > now,
            )
            .order_by(Subscription.ends_at.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


async def active_subscription_for_group(session: AsyncSession, chat_id: int) -> Subscription | None:
    owner_id = (
        await session.execute(
            select(GroupOwner.user_id).where(
                GroupOwner.chat_id == chat_id,
                GroupOwner.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if owner_id is None:
        return None
    return await active_subscription_for_owner(session, owner_id)


async def effective_limit_for_owner(
    session: AsyncSession,
    owner_user_id: int,
    limit_key: str,
) -> int | None:
    # A tariff slot is an owner-wide resource. Keep limit check + subsequent
    # mutation in the same transaction serialized across bot processes. The lock
    # is transaction-scoped and re-entrant, so reading several limits in one
    # transaction is safe and cheap.
    await session.execute(select(func.pg_advisory_xact_lock(int(owner_user_id))))

    subscription = await active_subscription_for_owner(session, owner_user_id)
    if subscription is None:
        return None

    tariff = (
        await session.execute(select(Tariff).where(Tariff.id == subscription.tariff_id))
    ).scalar_one_or_none()
    if tariff is None:
        return None

    raw_base = (tariff.limits_json or {}).get(limit_key)
    if raw_base is None:
        return None
    try:
        base = max(int(raw_base), 0)
    except (TypeError, ValueError):
        return None

    addon_quantity = (
        await session.execute(
            select(SubscriptionAddon.quantity).where(
                SubscriptionAddon.subscription_id == subscription.id,
                SubscriptionAddon.limit_key == limit_key,
            )
        )
    ).scalar_one_or_none()
    if addon_quantity is None:
        return base
    return base + max(int(addon_quantity), 0)


async def activate_test(session: AsyncSession, owner_user_id: int) -> tuple[Subscription | None, str]:
    active = await active_subscription_for_owner(session, owner_user_id)
    if active is not None:
        return active, "already_active"

    previous_trial = (
        await session.execute(
            select(Subscription.id).where(
                Subscription.owner_user_id == owner_user_id,
                Subscription.is_trial.is_(True),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if previous_trial is not None:
        return None, "trial_already_used"

    tariff = (
        await session.execute(
            select(Tariff).where(Tariff.code == "TEST", Tariff.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if tariff is None:
        return None, "trial_unavailable"

    now = datetime.now(timezone.utc)
    duration_days = tariff.duration_days or 3
    subscription = Subscription(
        owner_user_id=owner_user_id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.active.value,
        started_at=now,
        ends_at=now + timedelta(days=duration_days),
        is_trial=True,
    )
    session.add(subscription)
    await session.flush()
    await write_audit(
        session,
        "subscription.test_activated",
        actor_user_id=owner_user_id,
        target_type="subscription",
        target_id=str(subscription.id),
        payload={"tariff": "TEST", "ends_at": subscription.ends_at.isoformat()},
    )
    return subscription, "activated"


async def subscription_summary(session: AsyncSession, owner_user_id: int) -> tuple[Subscription | None, Tariff | None]:
    subscription = await active_subscription_for_owner(session, owner_user_id)
    if subscription is None:
        return None, None
    tariff = (
        await session.execute(select(Tariff).where(Tariff.id == subscription.tariff_id))
    ).scalar_one_or_none()
    return subscription, tariff
