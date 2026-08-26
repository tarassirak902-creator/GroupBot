from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection, AdvertisingMutualOpMember
from groupbot.routers.advertising_settlement import settlement_keyboard

logger = logging.getLogger(__name__)


async def process_mutual_op(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    completed: list[tuple[int, int, int, int, int, str | None, str, str, str, int, int, bool]] = []
    count = 0
    async with session_factory() as session:
        async with session.begin():
            directions = list((await session.execute(
                select(AdvertisingMutualOpDirection)
                .where(AdvertisingMutualOpDirection.status == "active")
                .with_for_update(skip_locked=True)
            )).scalars().all())
            for direction in directions:
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
                deal = (await session.execute(
                    select(AdvertisingDeal).where(AdvertisingDeal.id == direction.deal_id).with_for_update()
                )).scalar_one_or_none()
                if deal is None:
                    continue
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
    return count


async def advertising_mutual_lifecycle_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            completed = await process_mutual_op(bot, session_factory)
            if completed:
                logger.info("Completed %s mutual OP directions", completed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Mutual OP lifecycle iteration failed")
        await asyncio.sleep(30)
