from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminPermission, AdminRole
from groupbot.routers.group_control import KNOWN_PERMISSIONS


_ROLE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _role_target(callback_data: str) -> tuple[int, int] | None:
    prefixes = (
        "gctl:role:",
        "gctl:perm:",
        "gctl:perm_save:",
        "gctl:role_toggle:",
        "gctl:role_delete:",
        "gctl:role_delete_confirm:",
    )
    if not callback_data.startswith(prefixes):
        return None
    parts = callback_data.split(":")
    try:
        # All current role-editor callbacks store chat_id and role_id at [2:4].
        return int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        return None


def _is_permission_callback(callback_data: str) -> bool:
    return callback_data.startswith("gctl:perm:") or callback_data.startswith("gctl:perm_save:")


def _known_permission_keys() -> tuple[str, ...]:
    return tuple(key for key, _ in KNOWN_PERMISSIONS)


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
    persisted = {str(key): bool(value) for key, value in rows}
    return {key: bool(persisted.get(key, False)) for key in _known_permission_keys()}


def _normalized_draft(value: Any) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        return None
    return {key: bool(value.get(key, False)) for key in _known_permission_keys()}


class RoleEditorConsistencyMiddleware(BaseMiddleware):
    """Protect admin-role editor callbacks from stale or cross-role FSM drafts."""

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
        target = _role_target(callback_data)
        if target is None:
            return await handler(event, data)
        chat_id, role_id = target
        lock = _ROLE_LOCKS.setdefault((chat_id, role_id), asyncio.Lock())

        async with lock:
            state = data.get("state")
            if _is_permission_callback(callback_data) and state is not None:
                state_data = await state.get_data()
                draft_chat_id = state_data.get("permission_draft_chat_id")
                draft_role_id = state_data.get("permission_draft_role_id")
                draft = _normalized_draft(state_data.get("permission_draft"))
                base = _normalized_draft(state_data.get("permission_draft_base"))

                if draft_chat_id != chat_id or draft_role_id != role_id or draft is None:
                    await event.answer(
                        "Этот экран настройки устарел. Откройте нужный ранг заново.",
                        show_alert=True,
                    )
                    return None

                async with self.session_factory() as session:
                    current = await _permission_snapshot(
                        session,
                        chat_id=chat_id,
                        role_id=role_id,
                    )
                if current is None:
                    await state.clear()
                    await event.answer("Ранг уже удалён.", show_alert=True)
                    return None

                # Older/newly-created screens may not yet have a base snapshot.
                # They are safe only when their draft still equals DB state.
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

            # Refresh the optimistic base after opening/toggling/saving a role.
            if state is not None and callback_data.startswith((
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
                        current = await _permission_snapshot(
                            session,
                            chat_id=chat_id,
                            role_id=role_id,
                        )
                    if current is not None:
                        await state.update_data(permission_draft_base=current)

            return result
