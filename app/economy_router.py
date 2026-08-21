from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GroupSettings, GroupUser, Transaction


def create_economy_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="economy")

    async def economy_enabled(session: AsyncSession, chat_id: int) -> bool:
        result = await session.execute(select(GroupSettings).where(GroupSettings.chat_id == chat_id))
        settings = result.scalar_one_or_none()
        return bool(settings and settings.economy_enabled)

    @router.message(Command("balance"), F.chat.type.in_({"group", "supergroup"}))
    async def balance_handler(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if not await economy_enabled(session, message.chat.id):
                await message.answer("💰 Экономика отключена в этой группе.")
                return
            result = await session.execute(
                select(GroupUser).where(
                    GroupUser.chat_id == message.chat.id,
                    GroupUser.user_id == message.from_user.id,
                )
            )
            user = result.scalar_one_or_none()
            balance = user.balance if user else 0
        await message.answer(f"💰 Баланс: {balance}")

    @router.message(Command("pay"), F.chat.type.in_({"group", "supergroup"}))
    async def pay_handler(message: Message) -> None:
        if message.from_user is None:
            return
        if message.reply_to_message is None or message.reply_to_message.from_user is None:
            await message.answer("Ответь командой /pay <сумма> на сообщение получателя.")
            return
        target = message.reply_to_message.from_user
        if target.is_bot or target.id == message.from_user.id:
            await message.answer("Нельзя переводить валюту этому получателю.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: ответь /pay <сумма> на сообщение получателя.")
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await message.answer("Сумма должна быть целым числом.")
            return
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return

        async with session_factory() as session:
            async with session.begin():
                if not await economy_enabled(session, message.chat.id):
                    await message.answer("💰 Экономика отключена в этой группе.")
                    return
                sender_result = await session.execute(
                    select(GroupUser).where(
                        GroupUser.chat_id == message.chat.id,
                        GroupUser.user_id == message.from_user.id,
                    ).with_for_update()
                )
                receiver_result = await session.execute(
                    select(GroupUser).where(
                        GroupUser.chat_id == message.chat.id,
                        GroupUser.user_id == target.id,
                    ).with_for_update()
                )
                sender = sender_result.scalar_one_or_none()
                receiver = receiver_result.scalar_one_or_none()
                if sender is None or receiver is None:
                    await message.answer("Оба пользователя должны быть зарегистрированы в этой группе.")
                    return
                if sender.balance < amount:
                    await message.answer(f"Недостаточно средств. Твой баланс: {sender.balance}")
                    return
                sender.balance -= amount
                receiver.balance += amount
                session.add(Transaction(
                    chat_id=message.chat.id,
                    from_user_id=message.from_user.id,
                    to_user_id=target.id,
                    amount=amount,
                    kind="transfer",
                    reference=f"telegram_message:{message.message_id}",
                ))

        await message.answer(f"💸 Переведено {amount} пользователю {target.full_name}.")

    return router
