from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Group, GroupOwner, GroupSettings, GroupStatus, NetworkAdmin, User
from groupbot.moderation_models import ModerationAction
from groupbot.network_models import Network, NetworkGroup
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.moderation_notifications import unified_execute_action
from groupbot.services.protected_members import is_protected_member
from groupbot.services.subscriptions import active_subscription_for_group


NETWORK_COMMANDS = {"сбан", "сразбан", "сбанлист"}
NETWORK_COMMAND_RE = r"(?i)^\s*(?:сбан|сразбан|сбанлист)(?:\s+.*)?$"


def _command_parts(text: str | None) -> tuple[str | None, list[str]]:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return None, []
    parts = normalized.split(" ")
    command = parts[0].casefold()
    return (command if command in NETWORK_COMMANDS else None), parts[1:]


async def _network_for_chat(session: AsyncSession, chat_id: int) -> Network | None:
    rows = (
        await session.execute(
            select(Network)
            .join(NetworkGroup, NetworkGroup.network_id == Network.id)
            .join(
                GroupOwner,
                (GroupOwner.chat_id == NetworkGroup.chat_id)
                & (GroupOwner.user_id == Network.owner_user_id)
                & (GroupOwner.is_current.is_(True)),
            )
            .where(NetworkGroup.chat_id == chat_id)
            .order_by(Network.id.asc())
        )
    ).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def _network_group_ids(session: AsyncSession, network: Network) -> list[int]:
    return list(
        (
            await session.execute(
                select(NetworkGroup.chat_id)
                .join(Group, Group.chat_id == NetworkGroup.chat_id)
                .join(
                    GroupOwner,
                    (GroupOwner.chat_id == NetworkGroup.chat_id)
                    & (GroupOwner.user_id == network.owner_user_id)
                    & (GroupOwner.is_current.is_(True)),
                )
                .where(
                    NetworkGroup.network_id == network.id,
                    Group.status == GroupStatus.active.value,
                )
                .order_by(NetworkGroup.added_at.asc())
            )
        ).scalars().all()
    )


async def _network_permission(
    session: AsyncSession,
    *,
    network: Network,
    user_id: int,
    permission: str,
) -> bool:
    if network.owner_user_id == user_id:
        return True
    permissions = (
        await session.execute(
            select(NetworkAdmin.permissions_json).where(
                NetworkAdmin.owner_user_id == network.owner_user_id,
                NetworkAdmin.user_id == user_id,
                NetworkAdmin.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if permissions is None:
        return False
    allowed = {str(value) for value in (permissions or [])}
    return permission in allowed or "*" in allowed


async def _protected_in_group(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> bool:
    config = (
        await session.execute(
            select(GroupSettings.moderation_config).where(GroupSettings.chat_id == chat_id)
        )
    ).scalar_one_or_none() or {}
    if await is_protected_member(
        session,
        chat_id=chat_id,
        user_id=user_id,
        moderation_config=config,
    ):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    status = getattr(member.status, "value", str(member.status))
    return status in {"creator", "administrator"}


async def _send_network_action(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    group_ids: list[int],
    actor,
    target,
    action: str,
    reason: str | None,
) -> tuple[int, int, int]:
    applied = 0
    protected = 0
    failed = 0
    for chat_id in group_ids:
        try:
            if action == "ban":
                async with session_factory() as session:
                    if await _protected_in_group(
                        bot,
                        session,
                        chat_id=chat_id,
                        user_id=target.id,
                    ):
                        protected += 1
                        continue
            text = await unified_execute_action(
                bot=bot,
                session_factory=session_factory,
                chat_id=chat_id,
                actor=actor,
                target=target,
                action=action,
                reason=reason,
            )
            try:
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            applied += 1
        except Exception:
            failed += 1
    return applied, protected, failed


async def _render_network_banlist(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    network: Network,
    group_ids: list[int],
) -> None:
    if not group_ids:
        await message.reply("🌐 В этой сетке пока нет активных групп.")
        return
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    User,
                    func.count(func.distinct(ModerationAction.chat_id)).label("groups_count"),
                )
                .join(User, User.telegram_user_id == ModerationAction.target_user_id)
                .where(
                    ModerationAction.chat_id.in_(group_ids),
                    ModerationAction.action == "ban",
                    ModerationAction.is_active.is_(True),
                )
                .group_by(User.telegram_user_id)
                .order_by(func.count(func.distinct(ModerationAction.chat_id)).desc(), User.telegram_user_id.asc())
            )
        ).all()

    lines = ["🌐 <b>Сетевой банлист</b>", ""]
    if not rows:
        lines.append("Активных сетевых банов нет.")
    else:
        for index, row in enumerate(rows, start=1):
            line = f"{index}. {clickable_user_display(row[0])} — групп: <b>{row.groups_count}</b>"
            if len("\n".join([*lines, line])) > 3900:
                lines.extend(["", "Список сокращён из-за ограничения длины сообщения Telegram."])
                break
            lines.append(line)
    await message.reply(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def create_network_moderation_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="network_moderation")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(NETWORK_COMMAND_RE),
    )
    async def network_command(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        command, args = _command_parts(message.text)
        if command is None:
            return

        async with session_factory() as session:
            if await active_subscription_for_group(session, message.chat.id) is None:
                return
            network = await _network_for_chat(session, message.chat.id)
            if network is None:
                count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(NetworkGroup)
                            .where(NetworkGroup.chat_id == message.chat.id)
                        )
                    ).scalar_one()
                )
                if count == 0:
                    await message.reply("Эта группа не добавлена в сетку.")
                else:
                    await message.reply("Эта группа не входит в действующую сетку текущего владельца или сетка определена неоднозначно.")
                return
            permission = "unban" if command == "сразбан" else "ban"
            if not await _network_permission(
                session,
                network=network,
                user_id=message.from_user.id,
                permission=permission,
            ):
                await message.reply("Недостаточно сетевых прав Mimorus.")
                return
            group_ids = await _network_group_ids(session, network)

        if command == "сбанлист":
            await _render_network_banlist(message, session_factory, network, group_ids)
            return

        if message.reply_to_message is None or message.reply_to_message.from_user is None:
            await message.reply(
                "Используйте сетевую команду ответом на сообщение участника."
            )
            return
        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.reply("Бота нельзя выбрать целью сетевого наказания.")
            return
        if target.id == message.from_user.id:
            await message.reply("Нельзя применить сетевое наказание к себе.")
            return

        if command == "сбан":
            reason = " ".join(args).strip() or None
            applied, protected, failed = await _send_network_action(
                bot=bot,
                session_factory=session_factory,
                group_ids=group_ids,
                actor=message.from_user,
                target=target,
                action="ban",
                reason=reason,
            )
            await message.reply(
                "🌐 Сетевой бан выполнен.\n"
                f"Применено в группах: <b>{applied}</b>.\n"
                f"Пропущено из-за иммунитета/админ-статуса: <b>{protected}</b>.\n"
                f"Ошибок: <b>{failed}</b>.",
                parse_mode="HTML",
            )
            return

        applied, _, failed = await _send_network_action(
            bot=bot,
            session_factory=session_factory,
            group_ids=group_ids,
            actor=message.from_user,
            target=target,
            action="unban",
            reason=None,
        )
        await message.reply(
            "🌐 Сетевой разбан выполнен.\n"
            f"Обработано групп: <b>{applied}</b>.\n"
            f"Ошибок: <b>{failed}</b>.",
            parse_mode="HTML",
        )

    return router
