import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupStatus
from groupbot.services.audit import write_audit

logger = logging.getLogger(__name__)


async def group_lifecycle_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with session_factory() as session:
                due_ids = (await session.execute(
                    select(Group.chat_id).where(
                        ((Group.status == GroupStatus.pending.value) & (Group.connect_deadline_at.is_not(None)) & (Group.connect_deadline_at <= now))
                        | ((Group.status == GroupStatus.disabled.value) & (Group.disconnect_deadline_at.is_not(None)) & (Group.disconnect_deadline_at <= now))
                    )
                )).scalars().all()

            for chat_id in due_ids:
                previous_status = None
                should_leave = False
                async with session_factory() as session:
                    async with session.begin():
                        row = (await session.execute(
                            select(Group).where(Group.chat_id == chat_id).with_for_update()
                        )).scalar_one_or_none()
                        current = datetime.now(timezone.utc)
                        if row is None:
                            continue
                        pending_due = row.status == GroupStatus.pending.value and row.connect_deadline_at is not None and row.connect_deadline_at <= current
                        disabled_due = row.status == GroupStatus.disabled.value and row.disconnect_deadline_at is not None and row.disconnect_deadline_at <= current
                        if not (pending_due or disabled_due):
                            continue
                        previous_status = row.status
                        row.status = GroupStatus.left.value
                        row.connect_deadline_at = None
                        row.disconnect_deadline_at = None
                        await write_audit(
                            session,
                            "group.leave_deadline_reached",
                            chat_id=chat_id,
                            target_type="group",
                            target_id=str(chat_id),
                            payload={"previous_status": previous_status},
                        )
                        should_leave = True

                if not should_leave:
                    continue
                try:
                    if previous_status == GroupStatus.pending.value:
                        await bot.send_message(chat_id, "⚠️ Группа не была подключена владельцем за 1 минуту. Бот покидает группу.")
                    await bot.leave_chat(chat_id)
                except Exception:
                    logger.exception("Failed to leave chat_id=%s after lifecycle deadline", chat_id)
                    async with session_factory() as session:
                        async with session.begin():
                            row = (await session.execute(select(Group).where(Group.chat_id == chat_id).with_for_update())).scalar_one_or_none()
                            if row is not None and row.status == GroupStatus.left.value:
                                if previous_status == GroupStatus.pending.value:
                                    row.status = GroupStatus.pending.value
                                    row.connect_deadline_at = datetime.now(timezone.utc)
                                elif previous_status == GroupStatus.disabled.value:
                                    row.status = GroupStatus.disabled.value
                                    row.disconnect_deadline_at = datetime.now(timezone.utc)
                                await write_audit(
                                    session,
                                    "group.leave_failed",
                                    chat_id=chat_id,
                                    target_type="group",
                                    target_id=str(chat_id),
                                    payload={"previous_status": previous_status},
                                )
        except Exception:
            logger.exception("Group lifecycle worker iteration failed")
        await asyncio.sleep(10)
