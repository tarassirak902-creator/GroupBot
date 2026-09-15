from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualOp
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_owner

logger = logging.getLogger(__name__)


def _completion_text(op: AdvertisingManualOp, source_title: str, now: datetime) -> str:
    condition = (
        f"{op.quantity:,} участников".replace(",", " ")
        if op.mode == "subscribers"
        else f"{op.quantity} дней"
    )
    result = (
        f"📊 Результат: {op.progress_count:,}/{op.quantity:,}".replace(",", " ")
        if op.mode == "subscribers"
        else f"📅 Срок размещения: {op.quantity} дней"
    )
    return (
        "✅ <b>Реклама выполнена</b>\n\n"
        f"🅰️ Группа А: {escape(source_title)}\n"
        f"🅱️ Рекламная группа: {escape(op.target_title)}\n"
        f"🔗 Ссылка: {escape(op.target_url)}\n"
        f"📍 Условие: {condition}\n"
        f"{result}\n"
        f"🕐 Завершено: {now.strftime('%d.%m.%Y %H:%M')}"
    )


async def _notify_completion(
    bot: Bot,
    *,
    op: AdvertisingManualOp,
    source_title: str,
    target_owner_id: int | None,
    now: datetime,
) -> None:
    text = _completion_text(op, source_title, now)
    recipients = {op.owner_user_id}
    if target_owner_id is not None:
        recipients.add(target_owner_id)
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception(
                "Could not notify manual advertising completion op=%s user=%s",
                op.id,
                user_id,
            )


async def run_advertising_manual_lifecycle_once(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Complete manual OP at its target/expiry or stop it if source is unavailable."""
    now = datetime.now(timezone.utc)
    changed = 0
    notifications: list[tuple[AdvertisingManualOp, str, int | None]] = []

    async with session_factory() as session:
        async with session.begin():
            ops = list((await session.execute(
                select(AdvertisingManualOp)
                .where(AdvertisingManualOp.status == "active")
                .order_by(AdvertisingManualOp.id)
                .with_for_update(skip_locked=True)
            )).scalars().all())

            for op in ops:
                completed = (
                    op.mode == "days"
                    and op.ends_at is not None
                    and op.ends_at <= now
                ) or (
                    op.mode == "subscribers"
                    and op.progress_count >= op.quantity
                )
                if completed:
                    source_title = (await session.execute(
                        select(Group.title).where(Group.chat_id == op.source_chat_id)
                    )).scalar_one_or_none() or str(op.source_chat_id)
                    target_owner_id = None
                    if op.target_chat_id is not None:
                        target_owner_id = (await session.execute(
                            select(GroupOwner.user_id).where(
                                GroupOwner.chat_id == op.target_chat_id,
                                GroupOwner.is_current.is_(True),
                            )
                        )).scalar_one_or_none()
                    op.status = "completed"
                    op.completed_at = now
                    notifications.append((op, source_title, target_owner_id))
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

    # Send only after the DB transaction committed. Because only active rows are
    # transitioned above, later worker iterations cannot enqueue the same OP again.
    for op, source_title, target_owner_id in notifications:
        await _notify_completion(
            bot,
            op=op,
            source_title=source_title,
            target_owner_id=target_owner_id,
            now=now,
        )
    return changed


async def advertising_manual_lifecycle_worker(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int = 30,
) -> None:
    while True:
        try:
            await run_advertising_manual_lifecycle_once(bot, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Manual advertising lifecycle iteration failed")
        await asyncio.sleep(interval_seconds)
