import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GroupSettings
from app.rp_models import Cooldown, RPAction, RPTemplate

CONTENT_PATH = Path(__file__).resolve().parent.parent / "content" / "rp_defaults.json"


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


async def _ensure_actions(session: AsyncSession, chat_id: int) -> list[RPAction]:
    payload = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    for code, item in payload.items():
        await session.execute(
            insert(RPAction)
            .values(chat_id=chat_id, code=code, label=item["label"], emoji=item["emoji"])
            .on_conflict_do_nothing(constraint="uq_rp_actions_chat_code")
        )
    await session.flush()
    result = await session.execute(
        select(RPAction).where(RPAction.chat_id == chat_id, RPAction.is_active.is_(True)).order_by(RPAction.id)
    )
    actions = list(result.scalars())
    for action in actions:
        exists = await session.scalar(select(RPTemplate.id).where(RPTemplate.action_id == action.id).limit(1))
        if exists is None:
            for text in payload[action.code]["templates"]:
                session.add(RPTemplate(action_id=action.id, text=text, is_active=True))
    await session.flush()
    return actions


def create_rp_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="rp")

    @router.message(Command("rp"), F.chat.type.in_({"group", "supergroup"}))
    async def rp_menu(message: Message) -> None:
        if message.from_user is None:
            return
        target_message = message.reply_to_message
        if target_message is None or target_message.from_user is None:
            await message.answer("Ответь командой /rp на сообщение пользователя, с которым хочешь выполнить RP-действие.")
            return
        target = target_message.from_user
        if target.is_bot or target.id == message.from_user.id:
            await message.answer("Для этого RP-действия нужен другой пользователь группы.")
            return

        async with session_factory() as session:
            async with session.begin():
                settings = await session.scalar(select(GroupSettings).where(GroupSettings.chat_id == message.chat.id))
                if settings is None or not settings.rp_enabled:
                    await message.answer("🎭 RP отключён в этой группе.")
                    return
                actions = await _ensure_actions(session, message.chat.id)

        rows = [
            [InlineKeyboardButton(text=f"{a.emoji} {a.label}", callback_data=f"rp:{a.code}:{target.id}")]
            for a in actions
        ]
        await message.answer(f"🎭 Выбери действие для {target.full_name}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data.startswith("rp:"))
    async def rp_action(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None or callback.from_user is None:
            await callback.answer()
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Некорректное действие", show_alert=True)
            return
        _, code, target_raw = parts
        try:
            target_id = int(target_raw)
        except ValueError:
            await callback.answer("Некорректный пользователь", show_alert=True)
            return
        if target_id == callback.from_user.id:
            await callback.answer("Нужен другой пользователь", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        output: str | None = None
        cooldown_left: int | None = None

        async with session_factory() as session:
            async with session.begin():
                settings = await session.scalar(select(GroupSettings).where(GroupSettings.chat_id == callback.message.chat.id))
                if settings is None or not settings.rp_enabled:
                    await callback.answer("RP отключён", show_alert=True)
                    return
                action = await session.scalar(
                    select(RPAction).where(RPAction.chat_id == callback.message.chat.id, RPAction.code == code, RPAction.is_active.is_(True))
                )
                if action is None:
                    await callback.answer("Действие недоступно", show_alert=True)
                    return

                key = f"rp:{code}"
                cooldown = await session.scalar(
                    select(Cooldown).where(
                        Cooldown.chat_id == callback.message.chat.id,
                        Cooldown.user_id == callback.from_user.id,
                        Cooldown.key == key,
                    ).with_for_update()
                )
                if cooldown is not None and cooldown.expires_at > now:
                    cooldown_left = max(1, int((cooldown.expires_at - now).total_seconds()))
                else:
                    if cooldown is not None:
                        await session.delete(cooldown)
                    templates = list((await session.execute(
                        select(RPTemplate).where(RPTemplate.action_id == action.id, RPTemplate.is_active.is_(True))
                    )).scalars())
                    if not templates:
                        await callback.answer("Нет активных шаблонов для действия", show_alert=True)
                        return
                    target_member = await bot.get_chat_member(callback.message.chat.id, target_id)
                    text = random.choice(templates).text
                    output = text.replace("{Username1}", callback.from_user.full_name).replace("{Username2}", target_member.user.full_name)
                    if action.cooldown_seconds is not None and action.cooldown_seconds > 0:
                        expires = now + timedelta(seconds=action.cooldown_seconds)
                        await session.execute(
                            insert(Cooldown)
                            .values(chat_id=callback.message.chat.id, user_id=callback.from_user.id, key=key, expires_at=expires)
                            .on_conflict_do_update(constraint="uq_cooldown_scope", set_={"expires_at": expires})
                        )

        if cooldown_left is not None:
            await callback.answer(f"Кулдаун: ещё {cooldown_left} сек.", show_alert=True)
            return
        if output:
            await callback.message.answer(output)
            await callback.answer()

    @router.message(Command("rpcooldown"), F.chat.type.in_({"group", "supergroup"}))
    async def rp_cooldown(message: Message, bot: Bot) -> None:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Изменять RP-кулдауны могут только администраторы группы.")
            return
        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Формат: /rpcooldown <код_действия> <секунды|off>\nКоды можно посмотреть командой /rpactions")
            return
        code, raw = parts[1], parts[2].lower()
        if raw == "off":
            seconds = None
        else:
            try:
                seconds = int(raw)
            except ValueError:
                await message.answer("Кулдаун должен быть целым числом секунд или off.")
                return
            if seconds <= 0:
                await message.answer("Кулдаун должен быть больше нуля или off.")
                return
        async with session_factory() as session:
            async with session.begin():
                await _ensure_actions(session, message.chat.id)
                action = await session.scalar(select(RPAction).where(RPAction.chat_id == message.chat.id, RPAction.code == code))
                if action is None:
                    await message.answer("Неизвестный код RP-действия.")
                    return
                action.cooldown_seconds = seconds
                if seconds is None:
                    await session.execute(delete(Cooldown).where(Cooldown.chat_id == message.chat.id, Cooldown.key == f"rp:{code}"))
        await message.answer(f"✅ Кулдаун {code}: {'выключен' if seconds is None else str(seconds) + ' сек.'}")

    @router.message(Command("rpactions"), F.chat.type.in_({"group", "supergroup"}))
    async def rp_actions(message: Message) -> None:
        async with session_factory() as session:
            async with session.begin():
                actions = await _ensure_actions(session, message.chat.id)
        lines = [f"{a.emoji} {a.code} — {a.label} — кулдаун: {a.cooldown_seconds if a.cooldown_seconds else 'не задан'}" for a in actions]
        await message.answer("🎭 RP-действия:\n" + "\n".join(lines))

    return router
