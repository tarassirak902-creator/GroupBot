from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import AdminAssignment, Group, GroupOwner, GroupSettings, GroupStatus
from groupbot.network_models import Network, NetworkGroup
from groupbot.routers.content_filters import _lists
from groupbot.services.audit import write_audit
from groupbot.services.protection_schedule import schedule_config
from groupbot.services.subscriptions import active_subscription_for_owner, effective_limit_for_owner


async def _network_group_usage(session: AsyncSession, owner_id: int, network_id: int) -> tuple[int, int | None] | None:
    network = (await session.execute(select(Network.id).where(Network.id == network_id, Network.owner_user_id == owner_id))).scalar_one_or_none()
    if network is None:
        return None
    limit = await effective_limit_for_owner(session, owner_id, "network_groups_per_network")
    count = int((await session.execute(select(func.count()).select_from(NetworkGroup).where(NetworkGroup.network_id == network_id))).scalar_one())
    return count, limit


async def _network_group_limit_reached(session: AsyncSession, *, owner_id: int, network_id: int) -> tuple[bool, int | None]:
    usage = await _network_group_usage(session, owner_id, network_id)
    if usage is None:
        return False, None
    count, limit = usage
    return limit is not None and count >= limit, limit


async def _network_rows(session: AsyncSession, owner_id: int, network_id: int):
    return (await session.execute(
        select(Group.chat_id, Group.title)
        .join(NetworkGroup, NetworkGroup.chat_id == Group.chat_id)
        .join(GroupOwner, (GroupOwner.chat_id == NetworkGroup.chat_id) & (GroupOwner.user_id == owner_id) & (GroupOwner.is_current.is_(True)))
        .where(NetworkGroup.network_id == network_id)
        .order_by(NetworkGroup.added_at.asc(), Group.title.asc().nullslast())
    )).all()


async def _render_network_list(message: Message, session_factory: async_sessionmaker[AsyncSession], owner_id: int) -> bool:
    async with session_factory() as session:
        if await active_subscription_for_owner(session, owner_id) is None:
            return False
        rows = (await session.execute(
            select(Network.id, Network.name, func.count(GroupOwner.id).label("groups_count"))
            .outerjoin(NetworkGroup, NetworkGroup.network_id == Network.id)
            .outerjoin(GroupOwner, (GroupOwner.chat_id == NetworkGroup.chat_id) & (GroupOwner.user_id == owner_id) & (GroupOwner.is_current.is_(True)))
            .where(Network.owner_user_id == owner_id)
            .group_by(Network.id, Network.name).order_by(Network.id.asc())
        )).all()
        limit = await effective_limit_for_owner(session, owner_id, "networks")
    used = len(rows)
    usage = str(used) if limit is None else f"{used}/{limit}"
    lines = ["🌐 <b>Сетки групп</b>", "", f"Использовано сеток по тарифу: <b>{usage}</b>", "", "Ваши сетки:" if rows else "У вас пока нет сеток групп."]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        lines.append(f"• {row.name}: <b>{row.groups_count}</b> групп")
        buttons.append([InlineKeyboardButton(text=f"🌐 {row.name}"[:64], callback_data=f"networks:open:{row.id}")])
    if limit is None or used < limit:
        buttons.append([InlineKeyboardButton(text="➕ Создать сетку", callback_data="networks:create")])
    elif limit == 0:
        lines += ["", "Создание сеток недоступно на текущем тарифе."]
    elif used > limit:
        lines += ["", "⚠️ <b>Сеток больше лимита текущего тарифа.</b> Существующие сетки сохранены: их можно открывать, уменьшать и удалять. Создание новой станет доступно после уменьшения количества или повышения тарифа."]
    else:
        lines += ["", "Лимит сеток исчерпан. Существующие сетки можно открывать и удалять."]
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    await message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    return True


async def _render_network_card(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], network_id: int) -> bool:
    if callback.message is None:
        return False
    owner_id = callback.from_user.id
    async with session_factory() as session:
        network = (await session.execute(select(Network).where(Network.id == network_id, Network.owner_user_id == owner_id))).scalar_one_or_none()
        if network is None:
            return False
        groups = await _network_rows(session, owner_id, network_id)
        limit = await effective_limit_for_owner(session, owner_id, "network_groups_per_network")
    used = len(groups)
    usage = str(used) if limit is None else f"{used}/{limit}"
    lines = [f"🌐 <b>{network.name}</b>", "", f"Подключено групп по тарифу: <b>{usage}</b>"]
    can_add = limit is None or used < limit
    if limit is not None and used > limit:
        lines += ["", "⚠️ <b>Групп в сетке больше лимита текущего тарифа.</b> Уже подключённые группы сохранены и могут быть удалены. Добавление новой станет доступно после уменьшения количества или повышения тарифа."]
    elif limit is not None and used == limit:
        lines += ["", "Лимит групп в этой сетке исчерпан. Уже подключённые группы можно открывать и удалять."]
    buttons = [[InlineKeyboardButton(text="🌐 Подключенные группы", callback_data=f"networks:groups:{network_id}")]]
    if can_add:
        buttons.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data=f"networks:add:{network_id}")])
    if groups:
        buttons.append([InlineKeyboardButton(text="👮 Сетевые администраторы", callback_data=f"gctl:network_admins:{groups[0].chat_id}")])
    buttons += [
        [InlineKeyboardButton(text="🛡 Сетевая модерация", callback_data=f"networks:moderation:{network_id}")],
        [InlineKeyboardButton(text="🗑 Удалить сетку", callback_data=f"networks:delete:{network_id}")],
        [InlineKeyboardButton(text="◀️ Все сетки", callback_data="networks:list")],
    ]
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()
    return True


async def _render_network_groups(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession], network_id: int) -> bool:
    if callback.message is None:
        return False
    owner_id = callback.from_user.id
    async with session_factory() as session:
        network = (await session.execute(select(Network).where(Network.id == network_id, Network.owner_user_id == owner_id))).scalar_one_or_none()
        if network is None:
            return False
        groups = await _network_rows(session, owner_id, network_id)
        limit = await effective_limit_for_owner(session, owner_id, "network_groups_per_network")
    used = len(groups)
    usage = str(used) if limit is None else f"{used}/{limit}"
    lines = ["🌐 <b>Подключенные группы</b>", "", f"Сетка: <b>{network.name}</b>", f"Подключено групп по тарифу: <b>{usage}</b>"]
    buttons: list[list[InlineKeyboardButton]] = []
    if groups:
        lines += ["", "Выберите группу:"]
        for row in groups:
            buttons.append([InlineKeyboardButton(text=(row.title or "Группа без названия")[:64], callback_data=f"networks:group:{network_id}:{row.chat_id}")])
    else:
        lines += ["", "В этой сетке пока нет подключённых групп."]
    if limit is not None and used > limit:
        lines += ["", "⚠️ Чтобы снова добавлять группы, уменьшите количество до лимита тарифа или повысьте тариф."]
    buttons.append([InlineKeyboardButton(text="◀️ Назад к сетке", callback_data=f"networks:open:{network_id}")])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()
    return True


async def _reserve_limit_reached(session: AsyncSession, *, owner_id: int, chat_id: int) -> tuple[bool, int | None]:
    limit = await effective_limit_for_owner(session, owner_id, "reserve_admins")
    if limit is None:
        return False, None
    current = (await session.execute(
        select(AdminAssignment.id).join(GroupOwner, (GroupOwner.chat_id == AdminAssignment.chat_id) & (GroupOwner.user_id == owner_id) & (GroupOwner.is_current.is_(True)))
        .where(AdminAssignment.chat_id == chat_id, AdminAssignment.is_reserve.is_(True)).limit(1)
    )).scalar_one_or_none()
    if current is not None:
        return False, limit
    count = int((await session.execute(
        select(func.count()).select_from(AdminAssignment)
        .join(GroupOwner, (GroupOwner.chat_id == AdminAssignment.chat_id) & (GroupOwner.user_id == owner_id) & (GroupOwner.is_current.is_(True)))
        .where(AdminAssignment.is_reserve.is_(True))
    )).scalar_one())
    return count >= limit, limit


async def _schedule_limit_reached(session: AsyncSession, *, owner_id: int, chat_id: int) -> tuple[bool, int | None]:
    limit = await effective_limit_for_owner(session, owner_id, "protection_schedules")
    if limit is None:
        return False, None
    current = (await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id == chat_id))).scalar_one_or_none() or {}
    if schedule_config(current)["enabled"]:
        return False, limit
    configs = (await session.execute(
        select(GroupSettings.moderation_config).join(GroupOwner, (GroupOwner.chat_id == GroupSettings.chat_id) & (GroupOwner.user_id == owner_id) & (GroupOwner.is_current.is_(True)))
    )).scalars().all()
    return sum(1 for config in configs if schedule_config(config or {})["enabled"]) >= limit, limit


def create_tariff_limits_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="tariff_limits")

    @router.message(F.chat.type == "private", F.text == "🌐 Сетки групп")
    async def network_list_message(message: Message) -> None:
        if message.from_user is None:
            raise SkipHandler()
        async with session_factory() as session:
            has_subscription = await active_subscription_for_owner(session, message.from_user.id) is not None
        if not has_subscription:
            raise SkipHandler()
        sent = await message.answer("🌐 Сетки групп")
        await _render_network_list(sent, session_factory, message.from_user.id)

    @router.callback_query(F.data == "networks:list")
    async def network_list_callback(callback: CallbackQuery) -> None:
        if callback.message is None or not await _render_network_list(callback.message, session_factory, callback.from_user.id):
            raise SkipHandler()
        await callback.answer()

    @router.callback_query(F.data.startswith("networks:open:"))
    async def network_card(callback: CallbackQuery) -> None:
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            raise SkipHandler()
        if not await _render_network_card(callback, session_factory, network_id):
            raise SkipHandler()

    @router.callback_query(F.data.startswith("networks:groups:"))
    async def network_groups(callback: CallbackQuery) -> None:
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            raise SkipHandler()
        if not await _render_network_groups(callback, session_factory, network_id):
            raise SkipHandler()

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
            settings = (await session.execute(select(GroupSettings).where(GroupSettings.chat_id == chat_id))).scalar_one_or_none()
            lists = _lists(settings.moderation_config if settings else None, kind)
        if len(lists) >= limit:
            await callback.answer(f"Достигнут лимит списков текущего тарифа: {limit}.", show_alert=True)
            return
        raise SkipHandler()

    @router.callback_query(F.data.startswith("networks:add:"))
    async def network_add_screen_limit(callback: CallbackQuery) -> None:
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            raise SkipHandler()
        async with session_factory() as session:
            reached, limit = await _network_group_limit_reached(session, owner_id=callback.from_user.id, network_id=network_id)
        if reached and limit is not None:
            await callback.answer(f"В этой сетке достигнут лимит групп: {limit}.", show_alert=True)
            return
        raise SkipHandler()

    @router.callback_query(F.data.startswith("networks:add_group:"))
    async def network_add_group_limit(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            raise SkipHandler()
        try:
            network_id, chat_id = int(parts[2]), int(parts[3])
        except ValueError:
            raise SkipHandler()
        owner_id = callback.from_user.id
        async with session_factory() as session:
            async with session.begin():
                network = (await session.execute(
                    select(Network).where(Network.id == network_id, Network.owner_user_id == owner_id).with_for_update()
                )).scalar_one_or_none()
                if network is None:
                    await callback.answer("Сетка не найдена.", show_alert=True)
                    return

                limit = await effective_limit_for_owner(session, owner_id, "network_groups_per_network")
                exists = (await session.execute(
                    select(NetworkGroup.id).where(NetworkGroup.network_id == network_id, NetworkGroup.chat_id == chat_id)
                )).scalar_one_or_none()
                if exists is not None:
                    pass
                else:
                    owns_group = (await session.execute(
                        select(GroupOwner.id)
                        .join(Group, Group.chat_id == GroupOwner.chat_id)
                        .where(
                            GroupOwner.chat_id == chat_id,
                            GroupOwner.user_id == owner_id,
                            GroupOwner.is_current.is_(True),
                            Group.status == GroupStatus.active.value,
                        )
                        .limit(1)
                    )).scalar_one_or_none()
                    if owns_group is None:
                        await callback.answer("Эта группа недоступна для вашей сетки.", show_alert=True)
                        return

                    other_network = (await session.execute(
                        select(NetworkGroup.id)
                        .join(Network, Network.id == NetworkGroup.network_id)
                        .where(
                            NetworkGroup.chat_id == chat_id,
                            Network.owner_user_id == owner_id,
                            NetworkGroup.network_id != network_id,
                        )
                        .limit(1)
                    )).scalar_one_or_none()
                    if other_network is not None:
                        await callback.answer("Группа уже состоит в другой вашей сетке.", show_alert=True)
                        return

                    count = int((await session.execute(
                        select(func.count()).select_from(NetworkGroup).where(NetworkGroup.network_id == network_id)
                    )).scalar_one())
                    if limit is not None and count >= limit:
                        await callback.answer(f"В этой сетке достигнут лимит групп: {limit}.", show_alert=True)
                        return

                    session.add(NetworkGroup(network_id=network_id, chat_id=chat_id))
                    await write_audit(
                        session,
                        "network.group_added",
                        chat_id=chat_id,
                        actor_user_id=owner_id,
                        target_type="network",
                        target_id=str(network_id),
                    )
        if not await _render_network_card(callback, session_factory, network_id):
            await callback.answer("Сетка обновлена.")

    @router.callback_query(F.data.startswith("reserve:choose:"))
    async def reserve_choose_limit(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            raise SkipHandler()
        async with session_factory() as session:
            reached, limit = await _reserve_limit_reached(session, owner_id=callback.from_user.id, chat_id=chat_id)
        if reached and limit is not None:
            await callback.answer(f"Достигнут лимит резервных администраторов текущего тарифа: {limit}. Уже назначенные резервы сохраняются; снимите один из них или повысьте тариф.", show_alert=True)
            return
        raise SkipHandler()

    @router.callback_query(F.data.startswith("reserve:set:"))
    async def reserve_set_limit(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            raise SkipHandler()
        try:
            chat_id = int(parts[2])
        except ValueError:
            raise SkipHandler()
        async with session_factory() as session:
            reached, limit = await _reserve_limit_reached(session, owner_id=callback.from_user.id, chat_id=chat_id)
        if reached and limit is not None:
            await callback.answer(f"Достигнут лимит резервных администраторов текущего тарифа: {limit}. Уже назначенные резервы сохраняются; снимите один из них или повысьте тариф.", show_alert=True)
            return
        raise SkipHandler()

    @router.callback_query(F.data.startswith("ps:toggle:"))
    async def protection_schedule_limit(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            raise SkipHandler()
        async with session_factory() as session:
            reached, limit = await _schedule_limit_reached(session, owner_id=callback.from_user.id, chat_id=chat_id)
        if reached and limit is not None:
            await callback.answer(f"Достигнут лимит включённых расписаний защиты текущего тарифа: {limit}. Уже включённые расписания сохраняются; выключите одно из них или повысьте тариф.", show_alert=True)
            return
        raise SkipHandler()

    return router