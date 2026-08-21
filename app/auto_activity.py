import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)
CONTENT_PATH = Path(__file__).resolve().parent.parent / "content" / "auto_messages.json"


def _templates() -> list[dict[str, str]]:
    payload = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    return list(payload.get("templates") or [])


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


async def _ensure_settings(session: AsyncSession, chat_id: int) -> None:
    await session.execute(text("""
        INSERT INTO auto_event_settings(chat_id)
        VALUES (:chat_id)
        ON CONFLICT (chat_id) DO NOTHING
    """), {"chat_id": chat_id})


async def _run_for_group(bot: Bot, session_factory: async_sessionmaker[AsyncSession], chat_id: int, force: bool = False) -> bool:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        async with session.begin():
            enabled = (await session.execute(text("SELECT auto_activity_enabled FROM group_settings WHERE chat_id=:c"), {"c": chat_id})).scalar_one_or_none()
            if not enabled:
                return False
            await _ensure_settings(session, chat_id)
            row = (await session.execute(text("""
                SELECT min_interval_minutes, max_interval_minutes, activity_window_minutes,
                       next_run_at, last_template_key
                FROM auto_event_settings WHERE chat_id=:c FOR UPDATE
            """), {"c": chat_id})).mappings().one()
            if not force and row["next_run_at"] is not None and row["next_run_at"] > now:
                return False

            templates = _templates()
            if not templates:
                return False
            candidates_templates = [t for t in templates if t.get("key") != row["last_template_key"]] or templates
            chosen = random.choice(candidates_templates)
            rendered = chosen["text"]
            user_id = None

            if "{Username}" in rendered:
                cutoff = now - timedelta(minutes=row["activity_window_minutes"])
                candidates = (await session.execute(text("""
                    SELECT gu.user_id, COALESCE(NULLIF(u.first_name, ''), NULLIF(u.username, ''), 'Участник') AS display_name,
                           COALESCE(last_pick.cnt, 0) AS recent_picks
                    FROM group_users gu
                    JOIN users u ON u.user_id=gu.user_id
                    LEFT JOIN LATERAL (
                        SELECT COUNT(*) AS cnt FROM activity_events ae
                        WHERE ae.chat_id=gu.chat_id AND ae.user_id=gu.user_id
                          AND ae.created_at >= :cutoff
                    ) last_pick ON true
                    WHERE gu.chat_id=:c AND gu.last_activity_at >= :cutoff AND u.is_bot=false
                    ORDER BY recent_picks ASC, random()
                    LIMIT 1
                """), {"c": chat_id, "cutoff": cutoff})).mappings().first()
                if candidates is None:
                    neutral = [t for t in candidates_templates if "{Username}" not in t["text"]]
                    if not neutral:
                        return False
                    chosen = random.choice(neutral)
                    rendered = chosen["text"]
                else:
                    user_id = candidates["user_id"]
                    rendered = rendered.replace("{Username}", candidates["display_name"])

            await bot.send_message(chat_id, rendered)
            await session.execute(text("""
                INSERT INTO activity_events(chat_id, user_id, template_key)
                VALUES (:c, :u, :k)
            """), {"c": chat_id, "u": user_id, "k": chosen["key"]})
            next_minutes = random.randint(row["min_interval_minutes"], row["max_interval_minutes"])
            await session.execute(text("""
                UPDATE auto_event_settings
                SET next_run_at=:n, last_template_key=:k, updated_at=now()
                WHERE chat_id=:c
            """), {"n": now + timedelta(minutes=next_minutes), "k": chosen["key"], "c": chat_id})
    return True


async def auto_activity_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            async with session_factory() as session:
                chat_ids = (await session.execute(text("SELECT chat_id FROM group_settings WHERE auto_activity_enabled=true"))).scalars().all()
            for chat_id in chat_ids:
                try:
                    await _run_for_group(bot, session_factory, chat_id)
                except Exception:
                    logger.exception("Auto activity failed for chat_id=%s", chat_id)
        except Exception:
            logger.exception("Auto activity worker iteration failed")
        await asyncio.sleep(30)


def create_auto_activity_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="auto_activity")

    @router.message(Command("autoconfig"), F.chat.type.in_({"group", "supergroup"}))
    async def autoconfig(message: Message, bot: Bot) -> None:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Изменять автоактивности могут только администраторы.")
            return
        parts = (message.text or "").split()
        if len(parts) == 1:
            async with session_factory() as session:
                await _ensure_settings(session, message.chat.id)
                await session.commit()
                row = (await session.execute(text("SELECT min_interval_minutes,max_interval_minutes,activity_window_minutes FROM auto_event_settings WHERE chat_id=:c"), {"c": message.chat.id})).mappings().one()
            await message.answer(f"⚙️ Автоактивности: интервал {row['min_interval_minutes']}–{row['max_interval_minutes']} мин; окно активности {row['activity_window_minutes']} мин.\nФормат: /autoconfig <мин> <макс> <окно>")
            return
        if len(parts) != 4:
            await message.answer("Формат: /autoconfig <мин_интервал> <макс_интервал> <окно_активности_мин>")
            return
        try:
            min_i, max_i, window = map(int, parts[1:])
        except ValueError:
            await message.answer("Все значения должны быть целыми числами.")
            return
        if min_i < 1 or max_i < min_i or window < 1:
            await message.answer("Проверь значения: мин ≥ 1, макс ≥ мин, окно ≥ 1.")
            return
        async with session_factory() as session:
            async with session.begin():
                await _ensure_settings(session, message.chat.id)
                await session.execute(text("""
                    UPDATE auto_event_settings SET min_interval_minutes=:a,max_interval_minutes=:b,
                    activity_window_minutes=:w,next_run_at=NULL,updated_at=now() WHERE chat_id=:c
                """), {"a": min_i, "b": max_i, "w": window, "c": message.chat.id})
        await message.answer(f"✅ Автоактивности настроены: {min_i}–{max_i} мин, окно {window} мин.")

    @router.message(Command("autotest"), F.chat.type.in_({"group", "supergroup"}))
    async def autotest(message: Message, bot: Bot) -> None:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Команда доступна только администраторам.")
            return
        sent = await _run_for_group(bot, session_factory, message.chat.id, force=True)
        if not sent:
            await message.answer("Автоактивности отключены или нет доступного контента.")

    return router
