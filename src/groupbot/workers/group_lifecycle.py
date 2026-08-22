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
                due = (await session.execute(
                    select(Group).where(
                        ((Group.status == GroupStatus.pending.value) & (Group.connect_deadline_at <= now))
                        | ((Group.status == GroupStatus.disabled.value) & (Group.disconnect_deadline_at <= now))
                    )
                )).scalars().all()

            for group in due:
                try:
                    if group.status == GroupStatus.pending.value:
                        await bot.send_message(group.chat_id, "⚠️ Группа не была подключена владельцем за 1 минуту. Бот покидает группу.")
                    await bot.leave_chat(group.chat_id)
                    async with session_factory() as session:
                        async with session.begin():
                            row = (await session.execute(
                                select(Group).where(Group.chat_id == group.chat_id).with_for_update()
                            )).scalar_one_or_none()
                            if row is None:
                                continue
                            row.status = GroupStatus.left.value
                            row.connect_deadline_at = None
                            row.disconnect_deadline_at = None
                            await write_audit(
                                session,
                                "group.left",
                                chat_id=group.chat_id,
                                target_type="group",
                                target_id=str(group.chat_id),
                                payload={"previous_status": group.status},
                            )
                except Exception:
                    logger.exception("Failed group lifecycle action for chat_id=%s", group.chat_id)
        except Exception:
            logger.exception("Group lifecycle worker iteration failed")
        await asyncio.sleep(10)
