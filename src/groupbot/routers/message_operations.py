from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import GroupMember
from groupbot.moderation_models import ObservedMessage
from groupbot.routers.manual_moderation import _group_ready, _identity_from_tg
from groupbot.services.audit import write_audit
from groupbot.services.permissions import has_permission


async def cleanup_user_messages(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
    target_user_id: int,
    actor_user_id: int,
) -> tuple[int, int]:
    """Delete only messages Mimorus actually observed and Telegram still permits deleting."""
    async with session_factory() as session:
        rows = list((await session.execute(
            select(ObservedMessage.message_id)
            .where(
                ObservedMessage.chat_id == chat_id,
                ObservedMessage.user_id == target_user_id,
                ObservedMessage.deleted_at.is_(None),
            )
            .order_by(ObservedMessage.sent_at.desc())
        )).scalars().all())

    deleted_ids: list[int] = []
    for message_id in rows:
        try:
            await bot.delete_message(chat_id, message_id)
            deleted_ids.append(message_id)
        except Exception:
            continue

    if deleted_ids:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ObservedMessage)
                    .where(
                        ObservedMessage.chat_id == chat_id,
                        ObservedMessage.message_id.in_(deleted_ids),
                    )
                    .values(deleted_at=now)
                )
                member = (
                    await session.execute(
                        select(GroupMember)
                        .where(GroupMember.chat_id == chat_id, GroupMember.user_id == target_user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if member is not None:
                    member.deleted_messages += len(deleted_ids)
                await write_audit(
                    session,
                    "moderation.messages_cleaned",
                    chat_id=chat_id,
                    actor_user_id=actor_user_id,
                    target_type="user",
                    target_id=str(target_user_id),
                    payload={"deleted": len(deleted_ids), "attempted": len(rows)},
                )

    return len(deleted_ids), len(rows)


def create_message_operations_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="message_operations")

    @router.message(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*(?:очистить\s+пользователя|закрепи|открепи)\s*$"),
    )
    async def message_operation(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        command = " ".join((message.text or "").strip().split()).casefold()

        async with session_factory() as session:
            if not await _group_ready(session, message.chat.id):
                return
            permission = "delete" if command == "очистить пользователя" else "pin"
            if not await has_permission(session, message.chat.id, message.from_user.id, permission):
                await message.reply("Недостаточно прав Mimorus для этого действия.")
                return

        if command in {"закрепи", "открепи"}:
            if message.reply_to_message is None:
                verb = "закрепи" if command == "закрепи" else "открепи"
                await message.reply(f"Ответьте словом «{verb}» на нужное сообщение.")
                return
            try:
                if command == "закрепи":
                    await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
                else:
                    await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
            except Exception as exc:
                action = "закрепить" if command == "закрепи" else "открепить"
                await message.reply(f"Не удалось {action} сообщение через Telegram: {str(exc)[:200]}")
                return
            async with session_factory() as session:
                async with session.begin():
                    await write_audit(
                        session,
                        "moderation.message_pinned" if command == "закрепи" else "moderation.message_unpinned",
                        chat_id=message.chat.id,
                        actor_user_id=message.from_user.id,
                        target_type="message",
                        target_id=str(message.reply_to_message.message_id),
                        payload=None,
                    )
            await message.reply("📌 Сообщение закреплено." if command == "закрепи" else "📌 Сообщение откреплено.")
            return

        if message.reply_to_message is None or message.reply_to_message.from_user is None:
            await message.reply("Ответьте командой «очистить пользователя» на сообщение нужного пользователя.")
            return

        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.reply("Нельзя очищать сообщения бота этой командой.")
            return

        try:
            target_member = await bot.get_chat_member(message.chat.id, target.id)
            status = getattr(target_member.status, "value", str(target_member.status))
            if status == "creator" and target.id != message.from_user.id:
                await message.reply("Сообщения владельца группы нельзя очищать администраторам.")
                return
        except Exception:
            pass

        deleted, attempted = await cleanup_user_messages(
            bot=bot,
            session_factory=session_factory,
            chat_id=message.chat.id,
            target_user_id=target.id,
            actor_user_id=message.from_user.id,
        )
        target_text = _identity_from_tg(target)
        if attempted == 0:
            await message.answer(
                f"🧹 Для {target_text} у Mimorus пока нет сохранённых сообщений для очистки.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        await message.answer(
            f"🧹 Очистка сообщений {target_text} завершена.\n"
            f"Удалено через Telegram: <b>{deleted}</b> из <b>{attempted}</b> сохранённых сообщений.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    return router
