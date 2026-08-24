from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.network_models import Network, NetworkGroup
from groupbot.services.audit import write_audit
from groupbot.services.subscriptions import active_subscription_for_owner, effective_limit_for_owner


def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
    )


async def _owner_network(
    session: AsyncSession,
    network_id: int,
    owner_id: int,
) -> Network | None:
    return (
        await session.execute(
            select(Network).where(
                Network.id == network_id,
                Network.owner_user_id == owner_id,
            )
        )
    ).scalar_one_or_none()


async def _network_groups(
    session: AsyncSession,
    network_id: int,
    owner_id: int,
):
    return (
        await session.execute(
            select(Group.chat_id, Group.title)
            .join(NetworkGroup, NetworkGroup.chat_id == Group.chat_id)
            .join(
                GroupOwner,
                (GroupOwner.chat_id == NetworkGroup.chat_id)
                & (GroupOwner.user_id == owner_id)
                & (GroupOwner.is_current.is_(True)),
            )
            .where(NetworkGroup.network_id == network_id)
            .order_by(NetworkGroup.added_at.asc(), Group.title.asc().nullslast())
        )
    ).all()


async def _network_group(
    session: AsyncSession,
    network_id: int,
    owner_id: int,
    chat_id: int,
):
    return (
        await session.execute(
            select(Group.chat_id, Group.title)
            .join(NetworkGroup, NetworkGroup.chat_id == Group.chat_id)
            .join(
                GroupOwner,
                (GroupOwner.chat_id == NetworkGroup.chat_id)
                & (GroupOwner.user_id == owner_id)
                & (GroupOwner.is_current.is_(True)),
            )
            .where(
                NetworkGroup.network_id == network_id,
                NetworkGroup.chat_id == chat_id,
            )
            .limit(1)
        )
    ).first()


async def _render_list(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: int,
) -> None:
    async with session_factory() as session:
        subscription = await active_subscription_for_owner(session, owner_id)
        if subscription is None:
            await message.edit_text(
                "🌐 <b>Сетки групп</b>\n\n"
                "Для управления сетками нужен активный тариф.",
                parse_mode="HTML",
                reply_markup=_home_keyboard(),
            )
            return

        rows = (
            await session.execute(
                select(
                    Network.id,
                    func.count(GroupOwner.id).label("groups_count"),
                )
                .outerjoin(NetworkGroup, NetworkGroup.network_id == Network.id)
                .outerjoin(
                    GroupOwner,
                    (GroupOwner.chat_id == NetworkGroup.chat_id)
                    & (GroupOwner.user_id == owner_id)
                    & (GroupOwner.is_current.is_(True)),
                )
                .where(Network.owner_user_id == owner_id)
                .group_by(Network.id)
                .order_by(Network.id.asc())
            )
        ).all()
        limit = await effective_limit_for_owner(session, owner_id, "networks")

    lines = ["🌐 <b>Сетки групп</b>", ""]
    if rows:
        lines.append("Ваши сетки:")
    else:
        lines.append("У вас пока нет сеток групп.")

    buttons: list[list[InlineKeyboardButton]] = []
    for index, row in enumerate(rows, start=1):
        lines.append(f"• Сетка {index}: <b>{row.groups_count}</b> групп")
        buttons.append(
            [InlineKeyboardButton(text=f"🌐 Сетка {index}", callback_data=f"networks:open:{row.id}")]
        )

    if limit is None or len(rows) < limit:
        buttons.append([InlineKeyboardButton(text="➕ Создать сетку", callback_data="networks:create")])
    elif limit == 0:
        lines.extend(["", "Создание сеток недоступно на текущем тарифе."])
    else:
        lines.extend(["", f"Лимит текущего тарифа с дополнениями: <b>{limit}</b>."])

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    await message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def _render_card(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    network_id: int,
) -> None:
    if callback.message is None:
        return
    owner_id = callback.from_user.id
    async with session_factory() as session:
        network = await _owner_network(session, network_id, owner_id)
        if network is None:
            await callback.answer("Сетка не найдена.", show_alert=True)
            return
        groups = await _network_groups(session, network_id, owner_id)

    lines = [
        "🌐 <b>Сетка групп</b>",
        "",
        f"Подключено групп: <b>{len(groups)}</b>",
    ]

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🌐 Сетка", callback_data=f"networks:groups:{network_id}")],
        [InlineKeyboardButton(text="➕ Добавить группу", callback_data=f"networks:add:{network_id}")],
    ]
    if groups:
        buttons.append([
            InlineKeyboardButton(
                text="👮 Сетевые администраторы",
                callback_data=f"gctl:network_admins:{groups[0].chat_id}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="🛡 Сетевая модерация",
            callback_data=f"networks:moderation:{network_id}",
        )
    ])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить сетку", callback_data=f"networks:delete:{network_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Все сетки", callback_data="networks:list")])
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def _render_groups(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    network_id: int,
) -> None:
    if callback.message is None:
        return
    owner_id = callback.from_user.id
    async with session_factory() as session:
        if await _owner_network(session, network_id, owner_id) is None:
            await callback.answer("Сетка не найдена.", show_alert=True)
            return
        groups = await _network_groups(session, network_id, owner_id)

    lines = [
        "🌐 <b>Сетка</b>",
        "",
        f"Подключено групп: <b>{len(groups)}</b>",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    if groups:
        lines.extend(["", "Выберите группу:"])
        for row in groups:
            buttons.append([
                InlineKeyboardButton(
                    text=(row.title or "Группа без названия")[:64],
                    callback_data=f"networks:group:{network_id}:{row.chat_id}",
                )
            ])
    else:
        lines.extend(["", "В этой сетке пока нет подключённых групп."])

    buttons.append([InlineKeyboardButton(text="◀️ Назад к сетке", callback_data=f"networks:open:{network_id}")])
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def _render_group_card(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    network_id: int,
    chat_id: int,
) -> None:
    if callback.message is None:
        return
    owner_id = callback.from_user.id
    async with session_factory() as session:
        if await _owner_network(session, network_id, owner_id) is None:
            await callback.answer("Сетка не найдена.", show_alert=True)
            return
        group = await _network_group(session, network_id, owner_id, chat_id)

    if group is None:
        await callback.answer("Группа больше не состоит в этой сетке.", show_alert=True)
        await _render_groups(callback, session_factory, network_id)
        return

    title = group.title or "Группа без названия"
    await callback.message.edit_text(
        "🌐 <b>Группа в сетке</b>\n\n"
        f"🏠 <b>{title}</b>\n\n"
        "Группа подключена к этой сетке.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="➖ Удалить группу из сетки",
                        callback_data=f"networks:remove_confirm:{network_id}:{chat_id}",
                    )
                ],
                [InlineKeyboardButton(text="◀️ Назад к группам", callback_data=f"networks:groups:{network_id}")],
            ]
        ),
    )
    await callback.answer()


def create_networks_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="networks")

    @router.message(F.chat.type == "private", F.text == "🌐 Сетки групп")
    async def networks_menu(message: Message) -> None:
        if message.from_user is None:
            return
        sent = await message.answer("🌐 Сетки групп")
        await _render_list(sent, session_factory, message.from_user.id)

    @router.callback_query(F.data == "networks:list")
    async def networks_list(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        await _render_list(callback.message, session_factory, callback.from_user.id)
        await callback.answer()

    @router.callback_query(F.data == "networks:create")
    async def create_network(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        owner_id = callback.from_user.id
        async with session_factory() as session:
            async with session.begin():
                subscription = await active_subscription_for_owner(session, owner_id)
                if subscription is None:
                    await callback.answer("Сначала активируйте тариф.", show_alert=True)
                    return
                limit = await effective_limit_for_owner(session, owner_id, "networks")
                count = int(
                    (
                        await session.execute(
                            select(func.count()).select_from(Network).where(
                                Network.owner_user_id == owner_id
                            )
                        )
                    ).scalar_one()
                )
                if limit is not None and count >= limit:
                    await callback.answer("Достигнут лимит сеток текущего тарифа с дополнениями.", show_alert=True)
                    return
                network = Network(owner_user_id=owner_id)
                session.add(network)
                await session.flush()
                network_id = network.id
                await write_audit(
                    session,
                    "network.created",
                    actor_user_id=owner_id,
                    target_type="network",
                    target_id=str(network_id),
                )
        await _render_card(callback, session_factory, network_id)

    @router.callback_query(F.data.startswith("networks:open:"))
    async def open_network(callback: CallbackQuery) -> None:
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        await _render_card(callback, session_factory, network_id)

    @router.callback_query(F.data.startswith("networks:groups:"))
    async def network_groups(callback: CallbackQuery) -> None:
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        await _render_groups(callback, session_factory, network_id)

    @router.callback_query(F.data.startswith("networks:group:"))
    async def network_group_card(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            return
        try:
            network_id, chat_id = int(parts[2]), int(parts[3])
        except ValueError:
            return
        await _render_group_card(callback, session_factory, network_id, chat_id)

    @router.callback_query(F.data.startswith("networks:remove_confirm:"))
    async def remove_group_confirm(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            return
        try:
            network_id, chat_id = int(parts[2]), int(parts[3])
        except ValueError:
            return
        owner_id = callback.from_user.id
        async with session_factory() as session:
            if await _owner_network(session, network_id, owner_id) is None:
                await callback.answer("Сетка не найдена.", show_alert=True)
                return
            group = await _network_group(session, network_id, owner_id, chat_id)
        if group is None:
            await callback.answer("Группа больше не состоит в этой сетке.", show_alert=True)
            await _render_groups(callback, session_factory, network_id)
            return

        title = group.title or "Группа без названия"
        await callback.message.edit_text(
            "⚠️ <b>Удалить группу из сетки?</b>\n\n"
            f"🏠 <b>{title}</b>\n\n"
            "Группа останется подключённой к Mimorus, но перестанет входить в эту сетку. "
            "Сетевая модерация и права сетевых администраторов больше не будут действовать на неё через эту сетку.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Да, удалить",
                            callback_data=f"networks:remove_group:{network_id}:{chat_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=f"networks:group:{network_id}:{chat_id}",
                        )
                    ],
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("networks:moderation:"))
    async def network_moderation_info(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if await _owner_network(session, network_id, callback.from_user.id) is None:
                await callback.answer("Сетка не найдена.", show_alert=True)
                return
        await callback.message.edit_text(
            "🛡 <b>Сетевая модерация</b>\n\n"
            "Команды выполняются в одной из групп этой сетки ответом на сообщение участника:\n\n"
            "• <code>сбан причина</code> — бан во всех активных группах сетки;\n"
            "• <code>сразбан</code> — разбан во всех активных группах сетки;\n"
            "• <code>сбанлист</code> — активные баны по группам сетки.\n\n"
            "Сетевые администраторы используют только выданные им права ban/unban. "
            "Иммунитеты и действующие Telegram-администраторы сохраняют защиту.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад к сетке", callback_data=f"networks:open:{network_id}")]
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("networks:add:"))
    async def add_group_screen(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        owner_id = callback.from_user.id
        async with session_factory() as session:
            if await _owner_network(session, network_id, owner_id) is None:
                await callback.answer("Сетка не найдена.", show_alert=True)
                return
            occupied = set(
                (
                    await session.execute(
                        select(NetworkGroup.chat_id)
                        .join(Network, Network.id == NetworkGroup.network_id)
                        .where(
                            Network.owner_user_id == owner_id,
                            NetworkGroup.network_id != network_id,
                        )
                    )
                ).scalars().all()
            )
            existing = set(
                (
                    await session.execute(
                        select(NetworkGroup.chat_id).where(NetworkGroup.network_id == network_id)
                    )
                ).scalars().all()
            )
            owned = (
                await session.execute(
                    select(Group.chat_id, Group.title)
                    .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                    .where(
                        GroupOwner.user_id == owner_id,
                        GroupOwner.is_current.is_(True),
                        Group.status == GroupStatus.active.value,
                    )
                    .order_by(Group.title.asc().nullslast(), Group.chat_id.asc())
                )
            ).all()

        candidates = [row for row in owned if row.chat_id not in existing and row.chat_id not in occupied]
        buttons = [
            [
                InlineKeyboardButton(
                    text=(row.title or "Группа без названия")[:64],
                    callback_data=f"networks:add_group:{network_id}:{row.chat_id}",
                )
            ]
            for row in candidates
        ]
        buttons.append([InlineKeyboardButton(text="◀️ Назад к сетке", callback_data=f"networks:open:{network_id}")])
        text = (
            "➕ <b>Добавить группу в сетку</b>\n\n"
            + (
                "Выберите одну из ваших активных групп, которая ещё не состоит в другой сетке:"
                if candidates
                else "Нет доступных активных групп: они уже добавлены в эту или другую вашу сетку."
            )
        )
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("networks:add_group:"))
    async def add_group(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            return
        try:
            network_id, chat_id = int(parts[2]), int(parts[3])
        except ValueError:
            return
        owner_id = callback.from_user.id
        async with session_factory() as session:
            async with session.begin():
                if await _owner_network(session, network_id, owner_id) is None:
                    await callback.answer("Сетка не найдена.", show_alert=True)
                    return
                owns_group = (
                    await session.execute(
                        select(GroupOwner.id)
                        .join(Group, Group.chat_id == GroupOwner.chat_id)
                        .where(
                            GroupOwner.chat_id == chat_id,
                            GroupOwner.user_id == owner_id,
                            GroupOwner.is_current.is_(True),
                            Group.status == GroupStatus.active.value,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if owns_group is None:
                    await callback.answer("Эта группа недоступна для вашей сетки.", show_alert=True)
                    return
                other_network = (
                    await session.execute(
                        select(NetworkGroup.id)
                        .join(Network, Network.id == NetworkGroup.network_id)
                        .where(
                            NetworkGroup.chat_id == chat_id,
                            Network.owner_user_id == owner_id,
                            NetworkGroup.network_id != network_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if other_network is not None:
                    await callback.answer("Группа уже состоит в другой вашей сетке.", show_alert=True)
                    return
                exists = (
                    await session.execute(
                        select(NetworkGroup.id).where(
                            NetworkGroup.network_id == network_id,
                            NetworkGroup.chat_id == chat_id,
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(NetworkGroup(network_id=network_id, chat_id=chat_id))
                    await write_audit(
                        session,
                        "network.group_added",
                        chat_id=chat_id,
                        actor_user_id=owner_id,
                        target_type="network",
                        target_id=str(network_id),
                    )
        await _render_card(callback, session_factory, network_id)

    @router.callback_query(F.data.startswith("networks:remove_group:"))
    async def remove_group(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            return
        try:
            network_id, chat_id = int(parts[2]), int(parts[3])
        except ValueError:
            return
        owner_id = callback.from_user.id
        async with session_factory() as session:
            async with session.begin():
                if await _owner_network(session, network_id, owner_id) is None:
                    await callback.answer("Сетка не найдена.", show_alert=True)
                    return
                exists = (
                    await session.execute(
                        select(NetworkGroup.id).where(
                            NetworkGroup.network_id == network_id,
                            NetworkGroup.chat_id == chat_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if exists is None:
                    await callback.answer("Группа уже удалена из сетки.", show_alert=True)
                    return
                await session.execute(
                    delete(NetworkGroup).where(
                        NetworkGroup.network_id == network_id,
                        NetworkGroup.chat_id == chat_id,
                    )
                )
                await write_audit(
                    session,
                    "network.group_removed",
                    chat_id=chat_id,
                    actor_user_id=owner_id,
                    target_type="network",
                    target_id=str(network_id),
                )
        await callback.answer("Группа удалена из сетки")
        await _render_groups(callback, session_factory, network_id)

    @router.callback_query(F.data.startswith("networks:delete:"))
    async def delete_network(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            network_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        owner_id = callback.from_user.id
        async with session_factory() as session:
            async with session.begin():
                network = await _owner_network(session, network_id, owner_id)
                if network is None:
                    await callback.answer("Сетка уже удалена.", show_alert=True)
                    return
                await write_audit(
                    session,
                    "network.deleted",
                    actor_user_id=owner_id,
                    target_type="network",
                    target_id=str(network_id),
                )
                await session.delete(network)
        await _render_list(callback.message, session_factory, owner_id)
        await callback.answer("Сетка удалена")

    return router
