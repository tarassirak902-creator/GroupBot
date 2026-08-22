from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GroupSettings, Relationship, User


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def _rp_enabled(session: AsyncSession, chat_id: int) -> bool:
    result = await session.execute(select(GroupSettings).where(GroupSettings.chat_id == chat_id))
    settings = result.scalar_one_or_none()
    return bool(settings and settings.rp_enabled)


async def _married_relation(session: AsyncSession, chat_id: int, user_id: int) -> Relationship | None:
    result = await session.execute(
        select(Relationship).where(
            Relationship.chat_id == chat_id,
            Relationship.status == "married",
            or_(Relationship.user1_id == user_id, Relationship.user2_id == user_id),
        )
    )
    return result.scalar_one_or_none()


def create_relationship_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="relationships")

    @router.message(Command("propose"), F.chat.type.in_({"group", "supergroup"}))
    async def propose(message: Message) -> None:
        if message.from_user is None:
            return
        if message.reply_to_message is None or message.reply_to_message.from_user is None:
            await message.answer("Ответь командой /propose на сообщение пользователя.")
            return
        target = message.reply_to_message.from_user
        if target.is_bot or target.id == message.from_user.id:
            await message.answer("Нельзя сделать предложение этому пользователю.")
            return

        u1, u2 = _pair(message.from_user.id, target.id)
        async with session_factory() as session:
            async with session.begin():
                if not await _rp_enabled(session, message.chat.id):
                    await message.answer("RP-модуль отключён в этой группе.")
                    return
                if await _married_relation(session, message.chat.id, message.from_user.id):
                    await message.answer("У тебя уже есть активный брак в этой группе.")
                    return
                if await _married_relation(session, message.chat.id, target.id):
                    await message.answer("У этого пользователя уже есть активный брак в этой группе.")
                    return
                existing = await session.execute(
                    select(Relationship).where(
                        Relationship.chat_id == message.chat.id,
                        Relationship.user1_id == u1,
                        Relationship.user2_id == u2,
                    ).with_for_update()
                )
                rel = existing.scalar_one_or_none()
                if rel is not None and rel.status in {"pending", "married"}:
                    await message.answer("Между этими пользователями уже есть активное состояние отношений.")
                    return
                if rel is None:
                    rel = Relationship(chat_id=message.chat.id, user1_id=u1, user2_id=u2, proposer_id=message.from_user.id, status="pending")
                    session.add(rel)
                    await session.flush()
                else:
                    rel.proposer_id = message.from_user.id
                    rel.status = "pending"
                    await session.flush()
                relationship_id = rel.id

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💍 Принять", callback_data=f"relationship:accept:{relationship_id}"), InlineKeyboardButton(text="💔 Отказать", callback_data=f"relationship:reject:{relationship_id}")]])
        await message.answer(f"💍 {message.from_user.full_name} делает предложение {target.full_name}.\nРешение принимает только {target.full_name}.", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("relationship:"))
    async def relationship_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None:
            await callback.answer()
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or parts[1] not in {"accept", "reject"}:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        action = parts[1]
        try:
            relationship_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректное действие.", show_alert=True)
            return

        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(select(Relationship).where(Relationship.id == relationship_id).with_for_update())
                rel = result.scalar_one_or_none()
                if rel is None or rel.chat_id != callback.message.chat.id or rel.status != "pending":
                    await callback.answer("Это предложение уже неактивно.", show_alert=True)
                    return
                target_id = rel.user2_id if rel.proposer_id == rel.user1_id else rel.user1_id
                if callback.from_user.id != target_id:
                    await callback.answer("Ответить может только получатель предложения.", show_alert=True)
                    return
                if action == "accept":
                    if await _married_relation(session, rel.chat_id, rel.proposer_id or 0) or await _married_relation(session, rel.chat_id, target_id):
                        rel.status = "rejected"
                        await callback.answer("Один из участников уже состоит в браке.", show_alert=True)
                        return
                    rel.status = "married"
                else:
                    rel.status = "rejected"

        if action == "accept":
            await callback.message.edit_text("💍 Предложение принято. Теперь пользователи состоят в браке в этой группе.")
        else:
            await callback.message.edit_text("💔 Предложение отклонено.")
        await callback.answer()

    @router.message(Command("relationship"), F.chat.type.in_({"group", "supergroup"}))
    async def relationship_status(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            rel = await _married_relation(session, message.chat.id, message.from_user.id)
            if rel is None:
                await message.answer("💔 Активного брака в этой группе нет.")
                return
            spouse_id = rel.user2_id if rel.user1_id == message.from_user.id else rel.user1_id
            user_result = await session.execute(select(User).where(User.user_id == spouse_id))
            spouse = user_result.scalar_one_or_none()
            name = spouse.first_name if spouse and spouse.first_name else str(spouse_id)
        await message.answer(f"💍 Ты состоишь в браке с {name}.")

    @router.message(Command("divorce"), F.chat.type.in_({"group", "supergroup"}))
    async def divorce(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            async with session.begin():
                rel = await _married_relation(session, message.chat.id, message.from_user.id)
                if rel is None:
                    await message.answer("Активного брака для развода нет.")
                    return
                rel.status = "divorced"
                user1_result = await session.execute(select(User).where(User.user_id == rel.user1_id))
                user2_result = await session.execute(select(User).where(User.user_id == rel.user2_id))
                user1 = user1_result.scalar_one_or_none()
                user2 = user2_result.scalar_one_or_none()
                name1 = user1.first_name if user1 and user1.first_name else str(rel.user1_id)
                name2 = user2.first_name if user2 and user2.first_name else str(rel.user2_id)
        await message.answer(f"💔 Развод оформлен.\n{name1} и {name2} больше не женаты.")

    return router
