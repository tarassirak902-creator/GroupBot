from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.moderation_models import ModerationAction
from groupbot.routers.manual_moderation import (
    _duration,
    _execute_action as _base_execute_action,
    _format_expiry,
    _record_action as _base_record_action,
    _warning_count as _base_warning_count,
    _warning_limit,
    _warning_stage,
)
from groupbot.routers.user_display import clickable_identity
from groupbot.services.manual_punishment_access import manual_punishment_error
from groupbot.services.moderation_state import restore_member_permissions
from groupbot.services.permissions import is_group_owner


_CURRENT_MODERATION_SOURCE: ContextVar[str | None] = ContextVar(
    "mimorus_current_moderation_source",
    default=None,
)
_SERIALIZE_WARNING_COUNT: ContextVar[bool] = ContextVar(
    "mimorus_serialize_warning_count",
    default=False,
)


async def sourced_record_action(
    session: AsyncSession,
    *,
    chat_id: int,
    target_user_id: int,
    actor_user_id: int,
    action: str,
    reason: str | None = None,
    warning_index: int | None = None,
    expires_at: datetime | None = None,
    source: str = "manual",
) -> ModerationAction:
    """Record the action with the source of the current executor call."""
    effective_source = source
    if source == "manual":
        effective_source = _CURRENT_MODERATION_SOURCE.get() or source
    return await _base_record_action(
        session,
        chat_id=chat_id,
        target_user_id=target_user_id,
        actor_user_id=actor_user_id,
        action=action,
        reason=reason,
        warning_index=warning_index,
        expires_at=expires_at,
        source=effective_source,
    )


async def serialized_warning_count(
    session: AsyncSession,
    chat_id: int,
    target_user_id: int,
) -> int:
    """Lock warning read-modify-write in the executor's existing transaction."""
    if _SERIALIZE_WARNING_COUNT.get():
        lock_key = f"mimorus:warning:{chat_id}:{target_user_id}"
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
    return int(await _base_warning_count(session, chat_id, target_user_id))


def _notification_identity(user) -> str:
    """Clickable Telegram name for public moderation notifications, without @username."""
    return clickable_identity(
        telegram_user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=None,
    )


async def _actor_title(session: AsyncSession, chat_id: int, actor) -> str:
    identity = _notification_identity(actor)
    if getattr(actor, "is_bot", False):
        return identity
    if await is_group_owner(session, chat_id, actor.id):
        return f"Владелец группы {identity}"
    return f"Администратор {identity}"


def _reason_line(reason: str | None) -> str:
    return f"Причина: {escape(reason or 'не указана')}."


def _warning_punishment(count: int, limit: int) -> str | None:
    stage = _warning_stage(count, limit)
    if stage == "mute_15m":
        return "🔇 Автоматическое наказание: мут на 15 минут."
    if stage == "mute_1h":
        return "🔇 Автоматическое наказание: мут на 1 час."
    if stage == "ban":
        return "⛔ Автоматическое наказание: бан."
    return None


async def _clear_unban_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
    target_id: int,
) -> None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(ModerationAction)
                .where(
                    ModerationAction.chat_id == chat_id,
                    ModerationAction.target_user_id == target_id,
                    ModerationAction.action.in_(("ban", "mute", "warning")),
                    ModerationAction.is_active.is_(True),
                )
                .values(is_active=False, revoked_at=now)
            )


async def unified_execute_action(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    actor,
    target,
    action: str,
    reason: str | None,
    duration_token: str | None = None,
    source: str | None = None,
) -> str:
    """Run moderation and return a common public notification with clickable names only."""
    if action in {"warning", "mute", "ban"} and not getattr(actor, "is_bot", False):
        async with session_factory() as session:
            access_error = await manual_punishment_error(
                session,
                chat_id=chat_id,
                actor_id=actor.id,
                target_id=target.id,
            )
        if access_error:
            raise ValueError(access_error)

    source_token = _CURRENT_MODERATION_SOURCE.set(source) if source else None
    warning_token = _SERIALIZE_WARNING_COUNT.set(action == "warning")
    try:
        await _base_execute_action(
            bot=bot,
            session_factory=session_factory,
            chat_id=chat_id,
            actor=actor,
            target=target,
            action=action,
            reason=reason,
            duration_token=duration_token,
        )
    finally:
        _SERIALIZE_WARNING_COUNT.reset(warning_token)
        if source_token is not None:
            _CURRENT_MODERATION_SOURCE.reset(source_token)

    if action == "unmute":
        await restore_member_permissions(bot, chat_id, target.id)
    elif action == "unban":
        await _clear_unban_state(
            session_factory,
            chat_id=chat_id,
            target_id=target.id,
        )

    target_text = _notification_identity(target)
    async with session_factory() as session:
        actor_text = await _actor_title(session, chat_id, actor)
        limit = await _warning_limit(session, chat_id)
        warnings = min(await _base_warning_count(session, chat_id, target.id), limit)

    if action == "warning":
        punishment = _warning_punishment(warnings, limit)
        lines = [
            f"⚠️ {actor_text} выдал предупреждение {target_text}.",
            "",
            f"Активных предупреждений: {warnings} из {limit}.",
            _reason_line(reason),
        ]
        if punishment:
            lines.extend(["", punishment])
        lines.extend(["", "Будьте аккуратнее!"])
        return "\n".join(lines)

    if action == "mute":
        delta = _duration(duration_token or "")
        expiry = datetime.now(timezone.utc) + delta if delta is not None else None
        return "\n".join([
            f"🔇 {actor_text} выдал мут {target_text} до {_format_expiry(expiry)}.",
            "",
            f"Активных предупреждений: {warnings}.",
            _reason_line(reason),
        ])

    if action == "ban":
        return "\n".join([
            f"⛔ {actor_text} забанил {target_text}.",
            "",
            f"Активных предупреждений: {warnings}.",
            _reason_line(reason),
        ])

    if action == "unmute":
        return "\n".join([
            f"✅ {actor_text} снял мут с {target_text}.",
            "",
            f"Активных предупреждений: {warnings}.",
        ])

    if action == "unban":
        return "\n".join([
            f"✅ {actor_text} разбанил {target_text}.",
            "",
            f"Активных предупреждений: {warnings}.",
        ])

    raise ValueError("Неизвестное действие.")


# Legacy executor helpers are resolved through manual_moderation module globals.
# Install source-aware recording and transaction-local warning serialization once
# at import time without changing every existing caller.
from groupbot.routers import manual_moderation as _manual_moderation_module  # noqa: E402

_manual_moderation_module._record_action = sourced_record_action
_manual_moderation_module._warning_count = serialized_warning_count
