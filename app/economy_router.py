from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GroupSettings, GroupUser, Transaction


def create_economy_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="economy")

    async def economy_enabled(session: AsyncSession, chat_id: int) -> bool:
        result = await session.execute(select(GroupSettings).where(GroupSettings.chat_id == chat_id))
        settings = result.scalar_one_or_none()
        return bool(settings and settings.economy_enabled)

    async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"creator", "administrator"}

    @router.message(Command("balance"), F.chat.type.in_({"group", "supergroup"}))
    async def balance_handler(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_factory() as session:
            if not await economy_enabled(session, message.chat.id):
                await message.answer("💰 Экономика отключена в этой группе.")
                return
            result = await session.execute(
                select(GroupUser).where(GroupUser.chat_id == message.chat.id, GroupUser.user_id == message.from_user.id)
            )
            user = result.scalar_one_or_none()
            balance = user.balance if user else 0
        await message.answer(f"💰 Баланс: {balance}")

    @router.message(Command("money_add"), F.chat.type.in_({"group", "supergroup"}))
    async def money_add_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Эта команда доступна только администраторам группы.")
            return
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        if target is None or target.is_bot:
            await message.answer("Нельзя начислить валюту этому получателю.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: /money_add <сумма>. Можно ответом на сообщение пользователя.")
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
                result = await session.execute(
                    select(GroupUser).where(GroupUser.chat_id == message.chat.id, GroupUser.user_id == target.id).with_for_update()
                )
                user = result.scalar_one_or_none()
                if user is None:
                    await message.answer("Пользователь ещё не зарегистрирован в этой группе.")
                    return
                user.balance += amount
                session.add(Transaction(
                    chat_id=message.chat.id,
                    from_user_id=None,
                    to_user_id=target.id,
                    amount=amount,
                    kind="admin_grant",
                    reference=f"admin:{message.from_user.id};message:{message.message_id}",
                ))
        await message.answer(f"💰 Начислено {amount} пользователю {target.full_name}.")

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
                    select(GroupUser).where(GroupUser.chat_id == message.chat.id, GroupUser.user_id == message.from_user.id).with_for_update()
                )
                receiver_result = await session.execute(
                    select(GroupUser).where(GroupUser.chat_id == message.chat.id, GroupUser.user_id == target.id).with_for_update()
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

    @router.message(Command("transactions"), F.chat.type.in_({"group", "supergroup"}))
    async def transactions_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Эта команда доступна только администраторам группы.")
            return
        async with session_factory() as session:
            result = await session.execute(
                select(Transaction).where(Transaction.chat_id == message.chat.id).order_by(desc(Transaction.id)).limit(10)
            )
            rows = result.scalars().all()
        if not rows:
            await message.answer("Журнал транзакций пока пуст.")
            return
        lines = ["📒 Последние транзакции:"]
        for tx in rows:
            lines.append(f"#{tx.id} {tx.kind}: {tx.amount} | {tx.from_user_id or 'SYSTEM'} → {tx.to_user_id or 'SYSTEM'}")
        await message.answer("\n".join(lines))

    return router
