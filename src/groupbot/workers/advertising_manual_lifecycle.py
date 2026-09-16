from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualLink, AdvertisingManualOp
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group

logger = logging.getLogger(__name__)
NAVIGATION_LINK_MODE = "navigation"


def _group_link(title: str, url: str | None) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>' if url else escape(title)


def _result_text(op: AdvertisingManualOp) -> str:
    if op.mode == "subscribers":
        return f"🎯 Цель достигнута: <b>{op.progress_count:,}/{op.quantity:,} подписчиков</b>".replace(",", " ")
    if op.mode == "days":
        return f"⏱ Срок рекламы завершён: <b>{op.quantity} дней</b>"
    return "✅ Реклама завершена."


def _completion_text(op, source_title, target_title, source_url, target_url, recipient_kind):
    source = _group_link(source_title, source_url)
    target = _group_link(target_title, target_url)
    result = _result_text(op)
    if recipient_kind == "both":
        body = f"📢 Реклама между вашими группами завершена.\n\n📤 Рекламировала: {source}\n📥 Рекламировалась: {target}\n\n{result}\n\n🔗 Индивидуальная рекламная ссылка больше не используется."
    elif recipient_kind == "source":
        body = f"📢 Реклама {target} завершена.\n\n📤 Рекламировала: {source}\n📥 Рекламировалась: {target}\n\n{result}\n\n🔗 Рекламная ссылка больше не используется."
    else:
        body = f"📢 {source} завершила рекламу вашей группы.\n\n📤 Рекламировала: {source}\n📥 Рекламировалась: {target}\n\n{result}\n\n🔗 Индивидуальная рекламная ссылка больше не используется."
    return f"✅ <b>Реклама завершена</b>\n\n{body}"


async def _notify(bot, op, source_title, target_title, source_url, target_url, source_owner_id, target_owner_id):
    if source_owner_id is not None and source_owner_id == target_owner_id:
        recipients = [(source_owner_id, "both")]
    else:
        recipients = []
        if source_owner_id is not None:
            recipients.append((source_owner_id, "source"))
        if target_owner_id is not None:
            recipients.append((target_owner_id, "target"))
    for uid, kind in recipients:
        try:
            await bot.send_message(uid, _completion_text(op, source_title, target_title, source_url, target_url, kind), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("Could not notify manual advertising completion op=%s user=%s", op.id, uid)


async def _revoke_url(bot: Bot, chat_id: int | None, url: str) -> bool:
    """Revoke a Mimorus-created invite. Return True only when Telegram confirms it.

    Public @username/t.me links are not invite links and therefore do not need revocation.
    """
    if chat_id is None:
        return False
    if not url.startswith("https://t.me/+"):
        return True
    try:
        await bot.revoke_chat_invite_link(chat_id, url)
        return True
    except Exception:
        logger.exception("Could not revoke manual advertising invite chat=%s url=%s", chat_id, url)
        return False


async def _navigation_url(s: AsyncSession, chat_id: int | None) -> str | None:
    if chat_id is None:
        return None
    return (await s.execute(select(AdvertisingManualLink.invite_url).where(AdvertisingManualLink.target_chat_id == chat_id, AdvertisingManualLink.mode == NAVIGATION_LINK_MODE).order_by(AdvertisingManualLink.id).limit(1))).scalar_one_or_none()


async def _delete_link_after_revoke(session_factory: async_sessionmaker[AsyncSession], *, url: str) -> bool:
    async with session_factory() as s:
        async with s.begin():
            link = (await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.invite_url == url, AdvertisingManualLink.mode != NAVIGATION_LINK_MODE).with_for_update())).scalar_one_or_none()
            if link is None:
                return False
            active = (await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_url == url, AdvertisingManualOp.status == "active").limit(1))).scalar_one_or_none()
            if active is not None:
                return False
            await s.delete(link)
            return True


async def _retry_finished_link_cleanup(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Retry Telegram revocation for finished campaigns before forgetting their links.

    This deliberately keeps a failed invite in advertising_manual_links so the next
    lifecycle pass can retry instead of leaving a live, untracked OP invite behind.
    Navigation links are permanent UI links and are never touched here.
    """
    async with session_factory() as s:
        links = list((await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.mode != NAVIGATION_LINK_MODE).order_by(AdvertisingManualLink.id))).scalars().all())
    removed = 0
    for link in links:
        async with session_factory() as s:
            active = (await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_url == link.invite_url, AdvertisingManualOp.status == "active").limit(1))).scalar_one_or_none()
            finished = (await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_url == link.invite_url, AdvertisingManualOp.status.in_(("completed", "stopped"))).limit(1))).scalar_one_or_none()
        if active is not None or finished is None:
            continue
        if not await _revoke_url(bot, link.target_chat_id, link.invite_url):
            continue
        if await _delete_link_after_revoke(session_factory, url=link.invite_url):
            removed += 1
    return removed


async def _bot_admin(bot, chat_id):
    try:
        m = await bot.get_chat_member(chat_id, (await bot.get_me()).id)
        return m.status in {"administrator", "creator"}
    except Exception:
        return False


async def run_advertising_manual_lifecycle_once(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> int:
    now = datetime.now(timezone.utc)
    changed = 0
    notifications = []
    finished_ops: list[tuple[int, int | None, str]] = []

    async with session_factory() as s:
        async with s.begin():
            ops = list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status == "active").order_by(AdvertisingManualOp.id).with_for_update(skip_locked=True))).scalars().all())
            for op in ops:
                completed = (op.mode == "days" and op.ends_at is not None and op.ends_at <= now) or (op.mode == "subscribers" and op.progress_count >= op.quantity)
                source_status = (await s.execute(select(Group.status).where(Group.chat_id == op.source_chat_id))).scalar_one_or_none()
                source_owner = (await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id == op.source_chat_id, GroupOwner.is_current.is_(True)))).scalar_one_or_none()
                source_ok = source_status == GroupStatus.active.value and source_owner == op.owner_user_id and await active_subscription_for_group(s, op.source_chat_id) is not None
                target_owner = None
                target_ok = False
                if op.target_chat_id is not None:
                    target_status = (await s.execute(select(Group.status).where(Group.chat_id == op.target_chat_id))).scalar_one_or_none()
                    target_owner = (await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id == op.target_chat_id, GroupOwner.is_current.is_(True)))).scalar_one_or_none()
                    link = (await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.invite_url == op.target_url))).scalar_one_or_none()
                    target_ok = target_status == GroupStatus.active.value and target_owner is not None and (link is None or link.owner_user_id == target_owner)
                if completed:
                    source_title = (await s.execute(select(Group.title).where(Group.chat_id == op.source_chat_id))).scalar_one_or_none() or str(op.source_chat_id)
                    target_title = (await s.execute(select(Group.title).where(Group.chat_id == op.target_chat_id))).scalar_one_or_none() if op.target_chat_id is not None else None
                    target_title = target_title or op.target_title
                    source_url = await _navigation_url(s, op.source_chat_id)
                    target_url = await _navigation_url(s, op.target_chat_id)
                    op.status = "completed"
                    op.completed_at = now
                    notifications.append((op, source_title, target_title, source_url, target_url, source_owner, target_owner))
                    finished_ops.append((op.id, op.target_chat_id, op.target_url))
                    changed += 1
                    continue
                if not source_ok or not target_ok:
                    op.status = "stopped"
                    op.completed_at = now
                    finished_ops.append((op.id, op.target_chat_id, op.target_url))
                    changed += 1

    # Telegram/network calls happen outside DB transactions. A failed revoke leaves
    # the registered link in DB and will be retried by _retry_finished_link_cleanup.
    for op_id, target_chat_id, target_url in finished_ops:
        if await _revoke_url(bot, target_chat_id, target_url):
            if await _delete_link_after_revoke(session_factory, url=target_url):
                changed += 1
        else:
            logger.warning("Manual advertising link retained for retry op=%s", op_id)

    for op, source_title, target_title, source_url, target_url, source_owner, target_owner in notifications:
        await _notify(bot, op, source_title, target_title, source_url, target_url, source_owner, target_owner)

    # If Mimorus lost admin rights in the target, stop the campaign. We still retain
    # its registered invite until Telegram lets us revoke it successfully.
    async with session_factory() as s:
        active = list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status == "active", AdvertisingManualOp.target_chat_id.is_not(None)))).scalars().all())
    for op in active:
        if await _bot_admin(bot, op.target_chat_id):
            continue
        stopped = False
        async with session_factory() as s:
            async with s.begin():
                locked = (await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id == op.id, AdvertisingManualOp.status == "active").with_for_update())).scalar_one_or_none()
                if locked is not None:
                    locked.status = "stopped"
                    locked.completed_at = now
                    stopped = True
                    changed += 1
        if stopped and await _revoke_url(bot, op.target_chat_id, op.target_url):
            if await _delete_link_after_revoke(session_factory, url=op.target_url):
                changed += 1

    changed += await _retry_finished_link_cleanup(bot, session_factory)
    return changed


async def advertising_manual_lifecycle_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession], *, interval_seconds: int = 30) -> None:
    while True:
        try:
            await run_advertising_manual_lifecycle_once(bot, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Manual advertising lifecycle iteration failed")
        await asyncio.sleep(interval_seconds)
