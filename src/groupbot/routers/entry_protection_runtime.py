from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.entry_models import EntryCaptchaChallenge, EntryJoinEvent, EntryRaidState
from groupbot.models import GroupSettings
from groupbot.routers.entry_protection import (
    _antiraid_cfg,
    _apply_entry_action,
    _captcha_cfg,
    _captcha_challenge,
    _notify_raid,
    _render_captcha,
    _save_config,
)
from groupbot.routers.group_control import _ensure_group_settings, _owner_access
from groupbot.routers.manual_moderation import _group_ready, _unmuted_permissions
from groupbot.services.protected_members import is_protected_member

logger = logging.getLogger(__name__)


async def _group_default_permissions(bot: Bot, chat_id: int):
    try:
        chat = await bot.get_chat(chat_id)
        if chat.permissions is not None:
            return chat.permissions
    except Exception:
        logger.exception("Could not read group permissions chat_id=%s", chat_id)
    return _unmuted_permissions()


async def _restore_member_permissions(bot: Bot, chat_id: int, user_id: int) -> None:
    permissions = await _group_default_permissions(bot, chat_id)
    await bot.restrict_chat_member(chat_id, user_id, permissions=permissions)


async def _delete_challenge_message(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _cancel_group_challenges(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
) -> None:
    async with session_factory() as session:
        rows = list((await session.execute(
            select(EntryCaptchaChallenge).where(EntryCaptchaChallenge.chat_id == chat_id)
        )).scalars().all())
        if rows:
            async with session.begin_nested():
                await session.execute(
                    delete(EntryCaptchaChallenge).where(EntryCaptchaChallenge.chat_id == chat_id)
                )
            await session.commit()

    for row in rows:
        try:
            await _restore_member_permissions(bot, row.chat_id, row.user_id)
        except Exception:
            logger.info(
                "Could not restore permissions after captcha disable chat_id=%s user_id=%s",
                row.chat_id,
                row.user_id,
            )
        await _delete_challenge_message(bot, row.chat_id, row.message_id)


async def _captcha_timeout_task(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    challenge_id: int,
    message_id: int,
    deadline_at: datetime,
) -> None:
    wait = max(0.0, (deadline_at - datetime.now(timezone.utc)).total_seconds())
    if wait:
        await asyncio.sleep(wait)

    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(EntryCaptchaChallenge)
                    .where(EntryCaptchaChallenge.id == challenge_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.message_id != message_id or row.deadline_at != deadline_at:
                return
            if row.deadline_at > datetime.now(timezone.utc):
                return
            chat_id = row.chat_id
            user_id = row.user_id
            fail_action = row.fail_action
            try:
                await _apply_entry_action(bot, chat_id, user_id, fail_action)
            except Exception:
                logger.exception(
                    "Captcha timeout action failed chat_id=%s user_id=%s",
                    chat_id,
                    user_id,
                )
                return
            await session.delete(row)

    await _delete_challenge_message(bot, chat_id, message_id)


async def _start_challenge(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    message: Message,
    user,
    captcha: dict,
) -> None:
    chat_id = message.chat.id

    async with session_factory() as session:
        old = (
            await session.execute(
                select(EntryCaptchaChallenge).where(
                    EntryCaptchaChallenge.chat_id == chat_id,
                    EntryCaptchaChallenge.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        old_message_id = old.message_id if old is not None else None
        if old is not None:
            async with session.begin_nested():
                await session.delete(old)
            await session.commit()

    if old_message_id is not None:
        await _delete_challenge_message(bot, chat_id, old_message_id)

    try:
        await bot.restrict_chat_member(
            chat_id,
            user.id,
            permissions=(await _group_default_permissions(bot, chat_id)).model_copy(
                update={"can_send_messages": False}
            ),
        )
        challenge_text, markup, expected = _captcha_challenge(chat_id, user.id, captcha["mode"])
        challenge = await bot.send_message(
            chat_id,
            (
                f"🧩 <a href=\"tg://user?id={user.id}\">"
                f"{(user.full_name or 'Участник')}</a>, пройдите проверку.\n\n"
                f"{challenge_text}\n\n"
                f"На прохождение: <b>{captcha['timeout_seconds']} сек.</b>."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=int(captcha["timeout_seconds"]))

        async with session_factory() as session:
            async with session.begin():
                statement = (
                    insert(EntryCaptchaChallenge)
                    .values(
                        chat_id=chat_id,
                        user_id=user.id,
                        message_id=challenge.message_id,
                        expected_answer=expected,
                        deadline_at=deadline,
                        fail_action=str(captcha["fail_action"]),
                    )
                    .on_conflict_do_update(
                        constraint="uq_entry_captcha_chat_user",
                        set_={
                            "message_id": challenge.message_id,
                            "expected_answer": expected,
                            "deadline_at": deadline,
                            "fail_action": str(captcha["fail_action"]),
                            "created_at": func.now(),
                        },
                    )
                    .returning(EntryCaptchaChallenge.id)
                )
                challenge_id = int((await session.execute(statement)).scalar_one())

        asyncio.create_task(
            _captcha_timeout_task(
                bot,
                session_factory,
                challenge_id=challenge_id,
                message_id=challenge.message_id,
                deadline_at=deadline,
            )
        )
    except Exception:
        logger.exception("Could not start captcha chat_id=%s user_id=%s", chat_id, user.id)
        try:
            await _restore_member_permissions(bot, chat_id, user.id)
        except Exception:
            pass


async def restore_entry_protection_runtime(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(EntryRaidState).where(EntryRaidState.active_until <= now)
            )
            await session.execute(
                delete(EntryJoinEvent).where(EntryJoinEvent.joined_at < now - timedelta(days=1))
            )
        challenges = list((await session.execute(select(EntryCaptchaChallenge))).scalars().all())

    for row in challenges:
        asyncio.create_task(
            _captcha_timeout_task(
                bot,
                session_factory,
                challenge_id=row.id,
                message_id=row.message_id,
                deadline_at=row.deadline_at,
            )
        )

    if challenges:
        logger.info("Restored %s pending captcha challenge(s)", len(challenges))


def create_persistent_entry_runtime_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="persistent_entry_protection_runtime")

    @router.callback_query(F.data.startswith("entry:captcha_toggle:"))
    async def captcha_toggle(callback: CallbackQuery, bot: Bot) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        enabled = False
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                cfg = _captcha_cfg(settings.moderation_config)
                cfg["enabled"] = not cfg["enabled"]
                enabled = bool(cfg["enabled"])
                await _save_config(session, chat_id, "captcha", cfg)
        if not enabled:
            await _cancel_group_challenges(bot, session_factory, chat_id)
        await _render_captcha(callback, session_factory, chat_id)

    @router.callback_query(F.data.startswith("captcha:answer:"))
    async def captcha_answer(callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or "").split(":", 4)
        if len(parts) != 5:
            return
        try:
            chat_id = int(parts[2])
            user_id = int(parts[3])
        except ValueError:
            return
        answer = parts[4]
        if callback.from_user.id != user_id:
            await callback.answer("Эта капча предназначена другому участнику.", show_alert=True)
            return
        if callback.message is None:
            await callback.answer("Капча уже недействительна.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(EntryCaptchaChallenge)
                        .where(
                            EntryCaptchaChallenge.chat_id == chat_id,
                            EntryCaptchaChallenge.user_id == user_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None or row.message_id != callback.message.message_id:
                    await callback.answer("Капча уже недействительна.", show_alert=True)
                    return
                if row.deadline_at <= datetime.now(timezone.utc):
                    await callback.answer("Время на прохождение капчи истекло.", show_alert=True)
                    return
                if answer != row.expected_answer:
                    await callback.answer("❌ Неверный ответ. Попробуйте ещё раз.", show_alert=True)
                    return
                try:
                    await _restore_member_permissions(bot, chat_id, user_id)
                except Exception:
                    await callback.answer(
                        "Не удалось снять ограничения. Проверьте права бота.",
                        show_alert=True,
                    )
                    return
                await session.delete(row)

        await _delete_challenge_message(bot, chat_id, callback.message.message_id)
        await callback.answer("✅ Проверка пройдена.")

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
    async def new_members(message: Message, bot: Bot) -> None:
        if not message.new_chat_members:
            return

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            root = (
                await session.execute(
                    select(GroupSettings.moderation_config).where(
                        GroupSettings.chat_id == message.chat.id
                    )
                )
            ).scalar_one_or_none() or {}
            captcha = _captcha_cfg(root)
            raid = _antiraid_cfg(root)

        now = datetime.now(timezone.utc)
        raid_active = False
        raid_started = False
        raid_count = 0

        async with session_factory() as session:
            async with session.begin():
                state = (
                    await session.execute(
                        select(EntryRaidState)
                        .where(EntryRaidState.chat_id == message.chat.id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if state is not None and state.active_until <= now:
                    await session.delete(state)
                    state = None

                if raid["enabled"]:
                    cutoff = now - timedelta(seconds=int(raid["window_seconds"]))
                    await session.execute(
                        delete(EntryJoinEvent).where(
                            EntryJoinEvent.chat_id == message.chat.id,
                            EntryJoinEvent.joined_at < cutoff,
                        )
                    )
                    for user in message.new_chat_members:
                        if not user.is_bot:
                            session.add(
                                EntryJoinEvent(
                                    chat_id=message.chat.id,
                                    user_id=user.id,
                                    joined_at=now,
                                )
                            )
                    await session.flush()
                    raid_count = int((await session.execute(
                        select(func.count())
                        .select_from(EntryJoinEvent)
                        .where(
                            EntryJoinEvent.chat_id == message.chat.id,
                            EntryJoinEvent.joined_at >= cutoff,
                        )
                    )).scalar_one())

                    if state is None and raid_count >= int(raid["join_limit"]):
                        state = EntryRaidState(
                            chat_id=message.chat.id,
                            active_until=now + timedelta(seconds=int(raid["lock_seconds"])),
                        )
                        session.add(state)
                        raid_started = True
                    raid_active = state is not None and state.active_until > now

        if raid_started:
            await _notify_raid(
                bot,
                session_factory,
                chat_id=message.chat.id,
                count=raid_count,
                window_seconds=int(raid["window_seconds"]),
                lock_seconds=int(raid["lock_seconds"]),
            )

        for user in message.new_chat_members:
            if user.is_bot:
                continue
            async with session_factory() as session:
                protected = await is_protected_member(
                    session,
                    chat_id=message.chat.id,
                    user_id=user.id,
                    moderation_config=root,
                )
            if protected:
                continue
            if raid_active and raid["enabled"]:
                try:
                    await _apply_entry_action(bot, message.chat.id, user.id, str(raid["action"]))
                except Exception:
                    logger.exception(
                        "Anti-raid action failed chat_id=%s user_id=%s",
                        message.chat.id,
                        user.id,
                    )
                continue
            if captcha["enabled"]:
                await _start_challenge(
                    bot,
                    session_factory,
                    message=message,
                    user=user,
                    captcha=captcha,
                )

    return router
