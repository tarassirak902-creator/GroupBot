from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Achievement, UserAchievement


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in {"creator", "administrator"}


def create_achievement_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="achievements")

    async def require_admin(message: Message, bot: Bot) -> bool:
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Эта команда доступна только в группе.")
            return False
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Недостаточно прав.")
            return False
        return True

    @router.message(Command("achievement_help"))
    async def achievement_help(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        await message.answer(
            "🏆 Управление достижениями\n\n"
            "/achievement_add <code> <level> <название>\n"
            "/achievement_reward <code> <XP> <валюта>\n"
            "/achievement_description <code> <описание>\n"
            "/achievement_enabled <code> <on|off>\n"
            "/achievement_info <code>\n"
            "/achievement_list — все достижения группы\n"
            "/myachievements — мои полученные достижения"
        )

    @router.message(Command("achievement_add"))
    async def achievement_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 4:
            await message.answer("Формат: /achievement_add <code> <level> <название>")
            return
        _, code, raw_level, name = parts
        code = code.strip().lower()
        if not code or len(code) > 64 or not all(c.isalnum() or c in "_-" for c in code):
            await message.answer("Code: 1–64 символа; разрешены буквы, цифры, _ и -.")
            return
        try: level = int(raw_level)
        except ValueError: await message.answer("Уровень должен быть целым числом."); return
        if not 1 <= level <= 100000:
            await message.answer("Уровень должен быть от 1 до 100000."); return
        name = name.strip()
        if not name or len(name) > 255:
            await message.answer("Название должно содержать 1–255 символов."); return
        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    insert(Achievement).values(
                        chat_id=message.chat.id, code=code, name=name,
                        condition_type="level_reached", condition_value=level,
                        reward_xp=0, reward_currency=0, is_active=True,
                    ).on_conflict_do_nothing(constraint="uq_achievements_chat_code").returning(Achievement.id)
                )
                created_id = result.scalar_one_or_none()
        if created_id is None:
            await message.answer("Достижение с таким code уже существует в этой группе."); return
        await message.answer(f"🏅 Создано #{created_id}: {name}\nУсловие: уровень {level}.\nНаграды пока 0 XP / 0 валюты.")

    @router.message(Command("achievement_reward"))
    async def achievement_reward(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 4:
            await message.answer("Формат: /achievement_reward <code> <XP> <валюта>"); return
        code = parts[1].lower()
        try: reward_xp, reward_currency = int(parts[2]), int(parts[3])
        except ValueError: await message.answer("XP и валюта должны быть целыми числами."); return
        if reward_xp < 0 or reward_currency < 0:
            await message.answer("Награда не может быть отрицательной."); return
        async with session_factory() as session:
            row = (await session.execute(select(Achievement).where(Achievement.chat_id == message.chat.id, Achievement.code == code))).scalar_one_or_none()
            if row is None: await message.answer("Достижение не найдено."); return
            row.reward_xp, row.reward_currency = reward_xp, reward_currency
            await session.commit()
        await message.answer(f"✅ {code}: награда {reward_xp} XP / {reward_currency} валюты.")

    @router.message(Command("achievement_description"))
    async def achievement_description(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Формат: /achievement_description <code> <описание>"); return
        code, description = parts[1].lower(), parts[2].strip()
        if len(description) > 500:
            await message.answer("Описание не должно превышать 500 символов."); return
        async with session_factory() as session:
            row = (await session.execute(select(Achievement).where(Achievement.chat_id == message.chat.id, Achievement.code == code))).scalar_one_or_none()
            if row is None: await message.answer("Достижение не найдено."); return
            row.description = description or None; await session.commit()
        await message.answer("✅ Описание сохранено.")

    @router.message(Command("achievement_enabled"))
    async def achievement_enabled(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
            await message.answer("Формат: /achievement_enabled <code> <on|off>"); return
        code, enabled = parts[1].lower(), parts[2].lower() == "on"
        async with session_factory() as session:
            row = (await session.execute(select(Achievement).where(Achievement.chat_id == message.chat.id, Achievement.code == code))).scalar_one_or_none()
            if row is None: await message.answer("Достижение не найдено."); return
            row.is_active = enabled; await session.commit()
        await message.answer(f"✅ {code}: {'включено' if enabled else 'выключено'}.")

    @router.message(Command("achievement_info"))
    async def achievement_info(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Формат: /achievement_info <code>"); return
        async with session_factory() as session:
            row = (await session.execute(select(Achievement).where(Achievement.chat_id == message.chat.id, Achievement.code == parts[1].lower()))).scalar_one_or_none()
        if row is None: await message.answer("Достижение не найдено."); return
        await message.answer(
            f"🏆 {row.name} [{row.code}]\nID: {row.id} | {'on' if row.is_active else 'off'}\n"
            f"Описание: {row.description or '—'}\nУсловие: уровень ≥ {row.condition_value}\n"
            f"Награда: {row.reward_xp} XP / {row.reward_currency} валюты"
        )

    @router.message(Command("achievement_list"))
    async def achievement_list(message: Message) -> None:
        async with session_factory() as session:
            rows = (await session.execute(select(Achievement).where(Achievement.chat_id == message.chat.id).order_by(Achievement.condition_value, Achievement.id))).scalars().all()
        if not rows: await message.answer("Достижения в этой группе ещё не настроены."); return
        lines = ["🏆 Достижения группы:"]
        for row in rows:
            lines.append(f"#{row.id} {'✅' if row.is_active else '❌'} [{row.code}] {row.name} — lvl {row.condition_value}; +{row.reward_xp} XP; +{row.reward_currency} валюты")
        await message.answer("\n".join(lines))

    async def show_user_achievements(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"} or message.from_user is None: return
        async with session_factory() as session:
            rows = (await session.execute(
                select(Achievement.name, Achievement.code, UserAchievement.awarded_at)
                .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
                .where(UserAchievement.chat_id == message.chat.id, UserAchievement.user_id == message.from_user.id)
                .order_by(UserAchievement.awarded_at.asc())
            )).all()
        if not rows: await message.answer("🏅 Полученных достижений пока нет."); return
        lines = ["🏅 Твои достижения:"]
        for index, (name, code, _) in enumerate(rows, 1): lines.append(f"{index}. {name} [{code}]")
        await message.answer("\n".join(lines))

    @router.message(Command("achievements"))
    async def achievements(message: Message) -> None:
        await show_user_achievements(message)

    @router.message(Command("myachievements"))
    async def myachievements(message: Message) -> None:
        await show_user_achievements(message)

    return router
