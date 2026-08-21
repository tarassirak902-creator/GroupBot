from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import FilterItem, FilterSet, ModerationAction


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


def create_moderation_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="moderation")

    @router.message(Command("filterset_add"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_add(message: Message, bot: Bot) -> None:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Недостаточно прав.")
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3 or parts[1] not in {"word", "phrase"}:
            await message.answer("Формат: /filterset_add <word|phrase> <название>")
            return
        kind, name = parts[1], parts[2].strip()
        if not name:
            return
        async with session_factory() as session:
            async with session.begin():
                exists = await session.execute(select(FilterSet).where(FilterSet.chat_id == message.chat.id, FilterSet.name == name))
                if exists.scalar_one_or_none() is not None:
                    await message.answer("Набор с таким названием уже существует.")
                    return
                item = FilterSet(chat_id=message.chat.id, name=name, kind=kind, match_type="whole" if kind == "word" else "contains", action="delete", delete_message=True, exclude_admins=True)
                session.add(item)
                await session.flush()
                set_id = item.id
        await message.answer(f"🛡 Создан набор #{set_id}: {name} ({kind}), действие: удалить сообщение.")

    @router.message(Command("filteritem_add"), F.chat.type.in_({"group", "supergroup"}))
    async def filteritem_add(message: Message, bot: Bot) -> None:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Недостаточно прав.")
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Формат: /filteritem_add <ID_набора> <слово или фраза>")
            return
        try:
            set_id = int(parts[1])
        except ValueError:
            await message.answer("ID набора должен быть числом.")
            return
        value = parts[2].strip()
        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(select(FilterSet).where(FilterSet.id == set_id, FilterSet.chat_id == message.chat.id))
                filter_set = result.scalar_one_or_none()
                if filter_set is None:
                    await message.answer("Набор не найден в этой группе.")
                    return
                session.add(FilterItem(filter_set_id=set_id, value=value))
        await message.answer(f"✅ Добавлено в набор #{set_id}: {value}")

    @router.message(Command("filtersets"), F.chat.type.in_({"group", "supergroup"}))
    async def filtersets(message: Message) -> None:
        async with session_factory() as session:
            result = await session.execute(select(FilterSet).where(FilterSet.chat_id == message.chat.id).order_by(FilterSet.id))
            rows = result.scalars().all()
        if not rows:
            await message.answer("Наборов фильтра пока нет.")
            return
        lines = ["🛡 Наборы фильтра:"]
        for row in rows:
            lines.append(f"#{row.id} {'✅' if row.is_active else '❌'} {row.name} [{row.kind}] → {row.action}")
        await message.answer("\n".join(lines))

    @router.message(Command("modlog"), F.chat.type.in_({"group", "supergroup"}))
    async def modlog(message: Message, bot: Bot) -> None:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Недостаточно прав.")
            return
        async with session_factory() as session:
            result = await session.execute(select(ModerationAction).where(ModerationAction.chat_id == message.chat.id).order_by(ModerationAction.created_at.desc()).limit(10))
            rows = result.scalars().all()
        if not rows:
            await message.answer("Журнал модерации пока пуст.")
            return
        lines = ["📋 Последние срабатывания:"]
        for row in rows:
            lines.append(f"#{row.id} user={row.user_id} match={row.matched_value!r} action={row.action} telegram_ok={row.telegram_ok}")
        await message.answer("\n".join(lines))

    return router
