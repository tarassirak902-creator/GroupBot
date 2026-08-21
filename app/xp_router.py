from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GroupUser, XPConfig


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


async def _get_config(session: AsyncSession, chat_id: int) -> XPConfig | None:
    result = await session.execute(select(XPConfig).where(XPConfig.chat_id == chat_id))
    return result.scalar_one_or_none()


def _config_text(config: XPConfig | None) -> str:
    if config is None or config.xp_per_message is None or not config.level_thresholds:
        return (
            "⚙️ XP включён, но параметры прогрессии ещё не заданы.\n\n"
            "Администратор может задать их командой:\n"
            "/xpconfig <XP_за_сообщение> <пороги_через_запятую>\n\n"
            "Пример формата: /xpconfig X A,B,C\n"
            "где A — XP для 2 уровня, B — для 3 уровня и т.д."
        )
    thresholds = ", ".join(str(value) for value in config.level_thresholds)
    return (
        "⚙️ Настройки XP этой группы\n\n"
        f"XP за обычное сообщение: {config.xp_per_message}\n"
        f"Пороги уровней: {thresholds}\n\n"
        "Первый порог соответствует 2 уровню, второй — 3 уровню и далее."
    )


def create_xp_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="xp")

    @router.message(Command("xpconfig"))
    async def xp_config_handler(message: Message, bot: Bot) -> None:
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Эта команда доступна только в группе.")
            return
        if message.from_user is None:
            return
        if not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Настраивать XP могут только администраторы группы.")
            return

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) == 1:
            async with session_factory() as session:
                config = await _get_config(session, message.chat.id)
            await message.answer(_config_text(config))
            return

        if len(parts) != 3:
            await message.answer(
                "Формат: /xpconfig <XP_за_сообщение> <пороги_через_запятую>"
            )
            return

        try:
            xp_per_message = int(parts[1])
            thresholds = [int(value.strip()) for value in parts[2].split(",") if value.strip()]
        except ValueError:
            await message.answer("XP и все пороги должны быть целыми числами.")
            return

        if xp_per_message <= 0:
            await message.answer("XP за сообщение должен быть больше нуля.")
            return
        if not thresholds or any(value <= 0 for value in thresholds):
            await message.answer("Нужно указать хотя бы один положительный порог уровня.")
            return
        if thresholds != sorted(set(thresholds)):
            await message.answer("Пороги должны быть уникальными и идти строго по возрастанию.")
            return

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(XPConfig)
                    .values(
                        chat_id=message.chat.id,
                        xp_per_message=xp_per_message,
                        level_thresholds=thresholds,
                    )
                    .on_conflict_do_update(
                        index_elements=[XPConfig.chat_id],
                        set_={
                            "xp_per_message": xp_per_message,
                            "level_thresholds": thresholds,
                        },
                    )
                )
                config = await _get_config(session, message.chat.id)

        await message.answer("✅ Параметры XP сохранены.\n\n" + _config_text(config))

    @router.message(Command("rank"))
    async def rank_handler(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Эта команда доступна только в группе.")
            return
        if message.from_user is None:
            return

        async with session_factory() as session:
            user_result = await session.execute(
                select(GroupUser).where(
                    GroupUser.chat_id == message.chat.id,
                    GroupUser.user_id == message.from_user.id,
                )
            )
            group_user = user_result.scalar_one_or_none()
            config = await _get_config(session, message.chat.id)

        if group_user is None:
            await message.answer("Профиль в этой группе ещё не создан.")
            return

        next_threshold: int | None = None
        if config is not None and config.level_thresholds:
            next_threshold = next(
                (value for value in config.level_thresholds if value > group_user.xp),
                None,
            )

        text = f"🏆 Уровень: {group_user.level}\n✨ XP: {group_user.xp}"
        if next_threshold is not None:
            text += f"\n➡️ Следующий уровень: {next_threshold} XP"
        await message.answer(text)

    return router
