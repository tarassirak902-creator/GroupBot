from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupSettings
from groupbot.network_models import Network, NetworkGroup
from groupbot.routers.content_filters import _lists
from groupbot.services.subscriptions import effective_limit_for_owner


async def _network_group_limit_reached(
    session: AsyncSession,
    *,
    owner_id: int,
    network_id: int,
) -> tuple[bool, int | None]:
    network = (
        await session.execute(
            select(Network.id).where(
                Network.id == network_id,
                Network.owner_user_id == owner_id,
            )
        )
    ).scalar_one_or_none()
    if network is None:
        return False, None

    limit = await effective_limit_for_owner(
        session,
        owner_id,
        "network_groups_per_network",
    )
    if limit is None:
        return False, None

    count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(NetworkGroup)
                .where(NetworkGroup.network_id == network_id)
            )
        ).scalar_one()
    )
    return count >= limit, limit


def create_tariff_limits_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    """Tariff-only guards for existing feature routers.

    The guard never performs the underlying feature action. If the tariff
    allows the operation it raises SkipHandler so the already-existing router
    continues processing the callback.
    """

    router = Router(name="tariff_limits")

    @router.callback_query(F.data.startswith("cf:create_list:"))
    async def content_filter_list_limit(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            raise SkipHandler()
        kind = parts[2]
        try:
            chat_id = int(parts[3])
        except ValueError:
            raise SkipHandler()

        limit_key = "blocked_word_lists" if kind == "words" else "blocked_phrase_lists"
        async with session_factory() as session:
            limit = await effective_limit_for_owner(session, callback.from_user.id, limit_key)
            if limit is None:
                raise SkipHandler()
            settings = (
                await session.execute(
                    select(GroupSettings).where(GroupSettings.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            lists = _lists(settings.moderation_config if settings else None, kind)

        if len(lists) >= limit:
            await callback.answer(
                f"Достигнут лимит списков текущего тарифа: {limit}.",
                show_alert=True,
            )
            return
        raise SkipHandler()

    @router.callback_query(F.data.startswith("networks:add:"))
    async def network_add_screen_limit(callback: CallbackQuery) -> None:
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            raise SkipHandler()

        async with session_factory() as session:
            reached, limit = await _network_group_limit_reached(
                session,
                owner_id=callback.from_user.id,
                network_id=network_id,
            )
        if reached and limit is not None:
            await callback.answer(
                f"В этой сетке достигнут лимит групп: {limit}.",
                show_alert=True,
            )
            return
        raise SkipHandler()

    @router.callback_query(F.data.startswith("networks:add_group:"))
    async def network_add_group_limit(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            raise SkipHandler()
        try:
            network_id = int(parts[2])
        except ValueError:
            raise SkipHandler()

        async with session_factory() as session:
            reached, limit = await _network_group_limit_reached(
                session,
                owner_id=callback.from_user.id,
                network_id=network_id,
            )
        if reached and limit is not None:
            await callback.answer(
                f"В этой сетке достигнут лимит групп: {limit}.",
                show_alert=True,
            )
            return
        raise SkipHandler()

    return router
