from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timezone
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminPermission, AdminRole, GroupOwner, Subscription
from groupbot.routers.group_control import KNOWN_PERMISSIONS
from groupbot.routers.member_status_guard import is_regular_group_member
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_owner


SETTINGS_CALLBACK_PREFIXES = (
    "af:",
    "as:",
    "al:",
    "cf:",
    "entry:",
    "ps:",
    "preason:",
)
SPECIAL_PICK_PREFIX = "priv:special_pick:"
ROLE_EDITOR_PREFIXES = (
    "gctl:role:",
    "gctl:perm:",
    "gctl:perm_save:",
    "gctl:role_toggle:",
    "gctl:role_delete:",
    "gctl:role_delete_confirm:",
)

SUBSCRIPTION_EXEMPT_GROUP_PREFIXES = (
    "group:open:",
    "group:delete_prompt:",
    "group:delete_confirm:",
)
SUBSCRIPTION_EXEMPT_OWNER_CALLBACKS = {
    "networks:list",
}

EXPIRED_GROUP_CALLBACK_TEXT = (
    "⚠️ Активная подписка владельца группы закончилась. "
    "Функции и настройки Mimorus для этой группы временно недоступны. "
    "Продлите или активируйте тариф в личных сообщениях с ботом."
)
EXPIRED_OWNER_CALLBACK_TEXT = (
    "⚠️ Активная подписка закончилась. "
    "Эта функция Mimorus временно недоступна. "
    "Продлите или активируйте тариф в личных сообщениях с ботом."
)
STALE_SUBSCRIPTION_CALLBACK_TEXT = (
    "⚠️ Этот экран относится к предыдущему периоду подписки и уже устарел. "
    "Откройте группу или сетку заново в личном кабинете Mimorus."
)

_ROLE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _settings_chat_id(data: str) -> int | None:
    """Extract a Telegram group/supergroup id embedded in callback data."""
    for part in data.split(":"):
        try:
            value = int(part)
        except ValueError:
            continue
        if value < 0:
            return value
    return None


def _special_pick_target(data: str) -> int | None:
    parts = data.split(":", 4)
    if len(parts) != 5:
        return None
    try:
        return int(parts[4])
    except ValueError:
        return None


def _role_target(data: str) -> tuple[int, int] | None:
    if not data.startswith(ROLE_EDITOR_PREFIXES):
        return None
    parts = data.split(":")
    try:
        return int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        return None


def _permission_keys() -> tuple[str, ...]:
    return tuple(key for key, _ in KNOWN_PERMISSIONS)


def _normalize_permissions(value: Any) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        return None
    return {key: bool(value.get(key, False)) for key in _permission_keys()}


def _callback_predates_subscription(event: CallbackQuery, subscription: Subscription) -> bool:
    message = event.message
    message_date = getattr(message, "date", None)
    started_at = subscription.started_at
    if message_date is None or started_at is None:
        return False
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return message_date < started_at


async def _permission_snapshot(
    session: AsyncSession,
    *,
    chat_id: int,
    role_id: int,
) -> dict[str, bool] | None:
    role_exists = (
        await session.execute(
            select(AdminRole.id).where(
                AdminRole.id == role_id,
                AdminRole.chat_id == chat_id,
            )
        )
    ).scalar_one_or_none()
    if role_exists is None:
        return None
    rows = (
        await session.execute(
            select(AdminPermission.permission, AdminPermission.allowed).where(
                AdminPermission.role_id == role_id,
            )
        )
    ).all()
    persisted = {str(key): bool(allowed) for key, allowed in rows}
    return {key: bool(persisted.get(key, False)) for key in _permission_keys()}


async def _group_subscription_owner(
    session: AsyncSession,
    *,
    chat_id: int,
) -> tuple[int | None, Subscription | None]:
    owner_id = (
        await session.execute(
            select(GroupOwner.user_id).where(
                GroupOwner.chat_id == chat_id,
                GroupOwner.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if owner_id is None:
        return None, None
    return int(owner_id), await active_subscription_for_owner(session, int(owner_id))


class PrivateGroupSettingsAccessMiddleware(BaseMiddleware):
    """Guard private group callbacks, subscriptions and persistent editors."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        callback_data = event.data or ""
        chat_id_from_callback = _settings_chat_id(callback_data)

        if (
            chat_id_from_callback is not None
            and not callback_data.startswith(SUBSCRIPTION_EXEMPT_GROUP_PREFIXES)
        ):
            async with self.session_factory() as session:
                owner_id, subscription = await _group_subscription_owner(
                    session,
                    chat_id=chat_id_from_callback,
                )
            if owner_id is not None and subscription is None:
                await event.answer(EXPIRED_GROUP_CALLBACK_TEXT, show_alert=True)
                return None
            if subscription is not None and _callback_predates_subscription(event, subscription):
                await event.answer(STALE_SUBSCRIPTION_CALLBACK_TEXT, show_alert=True)
                return None

        if (
            callback_data.startswith("networks:")
            and callback_data not in SUBSCRIPTION_EXEMPT_OWNER_CALLBACKS
        ):
            async with self.session_factory() as session:
                subscription = await active_subscription_for_owner(session, event.from_user.id)
            if subscription is None:
                await event.answer(EXPIRED_OWNER_CALLBACK_TEXT, show_alert=True)
                return None
            if _callback_predates_subscription(event, subscription):
                await event.answer(STALE_SUBSCRIPTION_CALLBACK_TEXT, show_alert=True)
                return None

        is_settings = callback_data.startswith(SETTINGS_CALLBACK_PREFIXES)
        is_special_pick = callback_data.startswith(SPECIAL_PICK_PREFIX)
        role_target = _role_target(callback_data)
        if not is_settings and not is_special_pick and role_target is None:
            return await handler(event, data)

        if role_target is not None:
            chat_id, role_id = role_target
        else:
            chat_id = chat_id_from_callback
            if chat_id is None:
                await event.answer("Не удалось определить группу для этой настройки.", show_alert=True)
                return None
            role_id = None

        async with self.session_factory() as session:
            owner = await is_group_owner(session, chat_id, event.from_user.id)
            subscription = await active_subscription_for_owner(session, event.from_user.id) if owner else None
        if not owner:
            await event.answer(
                "Настройки этой группы доступны только её владельцу.",
                show_alert=True,
            )
            return None
        if subscription is None:
            await event.answer(EXPIRED_GROUP_CALLBACK_TEXT, show_alert=True)
            return None
        if _callback_predates_subscription(event, subscription):
            await event.answer(STALE_SUBSCRIPTION_CALLBACK_TEXT, show_alert=True)
            return None

        if is_special_pick:
            target_id = _special_pick_target(callback_data)
            bot = data.get("bot")
            if target_id is None or not isinstance(bot, Bot):
                await event.answer("Не удалось проверить выбранного участника.", show_alert=True)
                return None
            try:
                regular = await is_regular_group_member(bot, chat_id, target_id)
            except Exception:
                await event.answer("Не удалось перепроверить участника группы.", show_alert=True)
                return None
            if not regular:
                await event.answer(
                    "VIP и Недотрога назначаются только обычным участникам группы.",
                    show_alert=True,
                )
                return None

        if role_target is None:
            return await handler(event, data)

        lock = _ROLE_LOCKS.setdefault((chat_id, int(role_id)), asyncio.Lock())
        async with lock:
            state = data.get("state")
            permission_callback = callback_data.startswith(("gctl:perm:", "gctl:perm_save:"))
            if permission_callback:
                if not isinstance(state, FSMContext):
                    await event.answer("Редактор устарел. Откройте нужный ранг заново.", show_alert=True)
                    return None
                state_data = await state.get_data()
                draft = _normalize_permissions(state_data.get("permission_draft"))
                base = _normalize_permissions(state_data.get("permission_draft_base"))
                if (
                    state_data.get("permission_draft_chat_id") != chat_id
                    or state_data.get("permission_draft_role_id") != role_id
                    or draft is None
                ):
                    await event.answer(
                        "Эта кнопка относится к старому редактору. Откройте нужный ранг заново.",
                        show_alert=True,
                    )
                    return None

                async with self.session_factory() as session:
                    current = await _permission_snapshot(session, chat_id=chat_id, role_id=int(role_id))
                if current is None:
                    await state.clear()
                    await event.answer("Ранг уже удалён.", show_alert=True)
                    return None

                if base is None:
                    if draft != current:
                        await state.clear()
                        await event.answer(
                            "Права ранга уже изменились. Откройте ранг заново, чтобы не перезаписать новые настройки.",
                            show_alert=True,
                        )
                        return None
                    await state.update_data(permission_draft_base=current)
                elif base != current:
                    await state.clear()
                    await event.answer(
                        "Права ранга изменились в другом окне. Откройте ранг заново.",
                        show_alert=True,
                    )
                    return None

            result = await handler(event, data)

            if isinstance(state, FSMContext) and callback_data.startswith((
                "gctl:role:",
                "gctl:role_toggle:",
                "gctl:perm_save:",
            )):
                state_data = await state.get_data()
                if (
                    state_data.get("permission_draft_chat_id") == chat_id
                    and state_data.get("permission_draft_role_id") == role_id
                ):
                    async with self.session_factory() as session:
                        current = await _permission_snapshot(session, chat_id=chat_id, role_id=int(role_id))
                    if current is not None:
                        await state.update_data(permission_draft_base=current)

            return result
