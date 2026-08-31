from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingPlacement
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection, AdvertisingMutualOpMember
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.routers.advertising_settlement import settlement_keyboard
from groupbot.services.subscriptions import active_subscription_for_group

logger = logging.getLogger(__name__)


async def _owned_group_ready(
    session: AsyncSession,
    *,
    chat_id: int,
    owner_user_id: int,
) -> bool:
    row = (
        await session.execute(
            select(Group.chat_id)
            .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
            .where(
                Group.chat_id == chat_id,
                Group.status == GroupStatus.active.value,
                GroupOwner.user_id == owner_user_id,
                GroupOwner.is_current.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    return await active_subscription_for_group(session, chat_id) is not None


async def _mutual_failure_reason(session: AsyncSession, deal: AdvertisingDeal) -> str | None:
    terms = dict(deal.agreed_terms_json or {})
    if not terms.get("mutual_op"):
        return None
    try:
        buyer_chat_id = int(terms["group_a_chat_id"])
        seller_chat_id = int(terms["group_b_chat_id"])
    except (KeyError, TypeError, ValueError):
        return "данные групп взаимного ОП повреждены"
    if not await _owned_group_ready(
        session,
        chat_id=buyer_chat_id,
        owner_user_id=deal.buyer_user_id,
    ):
        return "группа покупателя отключена, сменила владельца или её подписка закончилась"
    if not await _owned_group_ready(
        session,
        chat_id=seller_chat_id,
        owner_user_id=deal.seller_user_id,
    ):
        return "группа рекламодателя отключена, сменила владельца или её подписка закончилась"
    return None


async def _mandatory_target_failure_reason(
    bot: Bot,
    session: AsyncSession,
    *,
    deal: AdvertisingDeal,
    placement: AdvertisingPlacement,
) -> str | None:
    cfg = dict(placement.config_json or {})
    target_chat_id = cfg.get("target_chat_id")
    if not isinstance(target_chat_id, int):
        return "в размещении отсутствует корректная целевая группа ОП"

    group = (
        await session.execute(select(Group).where(Group.chat_id == target_chat_id))
    ).scalar_one_or_none()
    if group is not None:
        current_owner = (
            await session.execute(
                select(GroupOwner.user_id).where(
                    GroupOwner.chat_id == target_chat_id,
                    GroupOwner.is_current.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if current_owner is None:
            return "целевая группа ОП больше не имеет текущего владельца в Mimorus"
        if int(current_owner) != deal.buyer_user_id:
            return "целевая группа ОП сменила владельца"
        if group.status != GroupStatus.active.value:
            return "целевая группа ОП больше не подключена к Mimorus"
        if await active_subscription_for_group(session, target_chat_id) is None:
            return "у владельца целевой группы ОП закончилась активная подписка Mimorus"
        return None

    # Manually entered targets are allowed even when they are not registered as
    # an owned Mimorus group. They still must remain reachable by the bot because
    # membership checks and subscriber progress depend on Telegram access.
    try:
        await bot.get_chat_member_count(target_chat_id)
    except Exception:
        return "Mimorus потерял доступ к внешней целевой группе ОП"
    return None


async def fail_unavailable_mandatory_targets(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    now = datetime.now(timezone.utc)
    notifications: list[tuple[int, int, int, str, bool]] = []
    failed = 0
    async with session_factory() as session:
        async with session.begin():
            rows = list((await session.execute(
                select(AdvertisingPlacement, AdvertisingDeal)
                .join(AdvertisingDeal, AdvertisingDeal.id == AdvertisingPlacement.deal_id)
                .where(
                    AdvertisingPlacement.kind == "mandatory",
                    AdvertisingPlacement.status == "active",
                    AdvertisingDeal.status == "accepted",
                )
                .with_for_update(skip_locked=True)
            )).all())
            for placement, deal in rows:
                reason = await _mandatory_target_failure_reason(
                    bot,
                    session,
                    deal=deal,
                    placement=placement,
                )
                if reason is None:
                    continue

                placement.status = "failed"
                cfg = dict(placement.config_json or {})
                cfg["failure_reason"] = reason
                cfg["failed_at"] = now.isoformat()
                placement.config_json = cfg
                failed += 1

                other_active = (
                    await session.execute(
                        select(AdvertisingPlacement.id).where(
                            AdvertisingPlacement.deal_id == deal.id,
                            AdvertisingPlacement.id != placement.id,
                            AdvertisingPlacement.status.in_(["pending", "ready", "active"]),
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                deal_finished = other_active is None
                if deal_finished:
                    deal.status = "finished_waiting_confirmation"
                    deal.finished_at = now
                notifications.append(
                    (deal.id, deal.buyer_user_id, deal.seller_user_id, reason, deal_finished)
                )

    for deal_id, buyer_id, seller_id, reason, deal_finished in notifications:
        text = (
            "⚠️ <b>Обязательная подписка остановлена</b>\n\n"
            f"Причина: <b>{escape(reason)}</b>\n\n"
            "Mimorus не считает эту часть рекламной сделки успешно выполненной."
        )
        markup = None
        if deal_finished:
            text += "\n\nСделка передана на подтверждение сторон: можно подтвердить отсутствие претензий или открыть спор."
            markup = settlement_keyboard(deal_id)
        else:
            text += "\n\nДругая активная часть сделки продолжает выполняться. После её завершения сделка перейдёт на подтверждение сторон."
        for user_id in (buyer_id, seller_id):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
    return failed


async def process_mutual_op(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    mandatory_failed = await fail_unavailable_mandatory_targets(bot, session_factory)
    now = datetime.now(timezone.utc)
    completed: list[tuple[int, int, int, int, int, str | None, str, str, str, int, int, bool]] = []
    failed: list[tuple[int, int, int, str, list[tuple[int, int, str | None]]]] = []
    failed_deals: set[int] = set()
    count = 0

    async with session_factory() as session:
        async with session.begin():
            directions = list((await session.execute(
                select(AdvertisingMutualOpDirection)
                .where(AdvertisingMutualOpDirection.status == "active")
                .with_for_update(skip_locked=True)
            )).scalars().all())
            for direction in directions:
                if direction.status != "active" or direction.deal_id in failed_deals:
                    continue
                deal = (await session.execute(
                    select(AdvertisingDeal)
                    .where(AdvertisingDeal.id == direction.deal_id)
                    .with_for_update()
                )).scalar_one_or_none()
                if deal is None:
                    direction.status = "failed"
                    direction.completed_at = now
                    continue
                if deal.status != "accepted":
                    direction.status = "failed"
                    direction.completed_at = now
                    continue

                failure_reason = await _mutual_failure_reason(session, deal)
                if failure_reason is not None:
                    all_active = list((await session.execute(
                        select(AdvertisingMutualOpDirection)
                        .where(
                            AdvertisingMutualOpDirection.deal_id == deal.id,
                            AdvertisingMutualOpDirection.status == "active",
                        )
                        .with_for_update()
                    )).scalars().all())
                    links: list[tuple[int, int, str | None]] = []
                    for item in all_active:
                        item.status = "failed"
                        item.completed_at = now
                        links.append((item.id, item.target_chat_id, item.invite_link))
                    deal.status = "finished_waiting_confirmation"
                    deal.finished_at = now
                    failed_deals.add(deal.id)
                    failed.append((deal.id, deal.buyer_user_id, deal.seller_user_id, failure_reason, links))
                    continue

                progress = 0
                should_finish = False
                if direction.mode == "days":
                    should_finish = direction.ends_at is not None and direction.ends_at <= now
                else:
                    progress = int((await session.execute(
                        select(func.count()).select_from(AdvertisingMutualOpMember).where(
                            AdvertisingMutualOpMember.direction_id == direction.id,
                            AdvertisingMutualOpMember.is_active.is_(True),
                        )
                    )).scalar_one())
                    should_finish = progress >= direction.quantity
                if not should_finish:
                    continue

                direction.status = "completed"
                direction.completed_at = now
                count += 1
                other_active = (await session.execute(
                    select(AdvertisingMutualOpDirection.id).where(
                        AdvertisingMutualOpDirection.deal_id == deal.id,
                        AdvertisingMutualOpDirection.id != direction.id,
                        AdvertisingMutualOpDirection.status == "active",
                    ).limit(1)
                )).scalar_one_or_none()
                deal_finished = other_active is None
                if deal_finished:
                    deal.status = "finished_waiting_confirmation"
                    deal.finished_at = now
                completed.append((
                    direction.id,
                    direction.target_chat_id,
                    deal.id,
                    deal.buyer_user_id,
                    deal.seller_user_id,
                    direction.invite_link,
                    direction.source_title,
                    direction.target_title,
                    direction.mode,
                    direction.quantity,
                    progress,
                    deal_finished,
                ))

    for deal_id, buyer_id, seller_id, reason, links in failed:
        for direction_id, target_chat_id, invite_link in links:
            if invite_link:
                try:
                    await bot.revoke_chat_invite_link(target_chat_id, invite_link)
                except Exception:
                    logger.info("Could not revoke failed mutual OP invite link direction=%s", direction_id)
        text = (
            "⚠️ <b>Взаимное ОП остановлено</b>\n\n"
            f"Причина: <b>{escape(reason)}</b>\n\n"
            "Mimorus не считает сделку успешно выполненной. Вы можете подтвердить отсутствие претензий или открыть спор."
        )
        for user_id in (buyer_id, seller_id):
            try:
                await bot.send_message(
                    user_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=settlement_keyboard(deal_id),
                )
            except Exception:
                pass

    for direction_id, target_chat_id, deal_id, buyer_id, seller_id, invite_link, source_title, target_title, mode, quantity, progress, deal_finished in completed:
        if invite_link:
            try:
                await bot.revoke_chat_invite_link(target_chat_id, invite_link)
            except Exception:
                logger.info("Could not revoke completed mutual OP invite link direction=%s", direction_id)

        result = f"{quantity} дн." if mode == "days" else f"{progress}/{quantity} участников"
        text = (
            "✅ <b>Одно направление взаимного ОП выполнено</b>\n\n"
            f"🏠 {source_title} → {target_title}\n"
            f"🎯 Результат: <b>{result}</b>\n\n"
            "ОП в группе-источнике остановлено. Второе направление продолжит работать, если ещё не выполнило условие."
        )
        if deal_finished:
            text += "\n\n🏁 Обе стороны выполнили условия. Сделка завершена."
        for user_id in (buyer_id, seller_id):
            try:
                await bot.send_message(
                    user_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=settlement_keyboard(deal_id) if deal_finished else None,
                )
            except Exception:
                pass
    return mandatory_failed + count + len(failed)


async def advertising_mutual_lifecycle_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            processed = await process_mutual_op(bot, session_factory)
            if processed:
                logger.info("Processed %s advertising OP lifecycle events", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Mutual/mandatory OP lifecycle iteration failed")
        await asyncio.sleep(30)
