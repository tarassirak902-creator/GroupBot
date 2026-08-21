from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import FilterItem, FilterSet, ModerationAction, ModerationWarning, ModerationWhitelist


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


def create_moderation_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="moderation")

    async def require_admin(message: Message, bot: Bot) -> bool:
        if message.from_user is None or not await _is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("Недостаточно прав.")
            return False
        return True

    async def get_set(session: AsyncSession, chat_id: int, set_id: int) -> FilterSet | None:
        return (await session.execute(select(FilterSet).where(FilterSet.id == set_id, FilterSet.chat_id == chat_id))).scalar_one_or_none()

    @router.message(Command("filterset_add"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3 or parts[1] not in {"word", "phrase"}:
            await message.answer("Формат: /filterset_add <word|phrase> <название>"); return
        kind, name = parts[1], parts[2].strip()
        async with session_factory() as session:
            if (await session.execute(select(FilterSet).where(FilterSet.chat_id == message.chat.id, FilterSet.name == name))).scalar_one_or_none():
                await message.answer("Набор с таким названием уже существует."); return
            item = FilterSet(chat_id=message.chat.id, name=name, kind=kind, match_type="whole" if kind == "word" else "contains", action="delete", delete_message=True, exclude_admins=True, exclude_whitelist=True)
            session.add(item); await session.commit(); await session.refresh(item)
        await message.answer(f"🛡 Создан набор #{item.id}: {name} ({kind}), действие: delete.")

    @router.message(Command("filteritem_add"), F.chat.type.in_({"group", "supergroup"}))
    async def filteritem_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Формат: /filteritem_add <ID_набора> <слово или фраза>"); return
        try: set_id = int(parts[1])
        except ValueError:
            await message.answer("ID набора должен быть числом."); return
        value = parts[2].strip()
        async with session_factory() as session:
            if await get_set(session, message.chat.id, set_id) is None:
                await message.answer("Набор не найден в этой группе."); return
            exists = (await session.execute(select(FilterItem).where(FilterItem.filter_set_id == set_id, FilterItem.value == value))).scalar_one_or_none()
            if exists:
                await message.answer("Такой элемент уже есть в наборе."); return
            session.add(FilterItem(filter_set_id=set_id, value=value)); await session.commit()
        await message.answer(f"✅ Добавлено в набор #{set_id}: {value}")

    @router.message(Command("filteritem_remove"), F.chat.type.in_({"group", "supergroup"}))
    async def filteritem_remove(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Формат: /filteritem_remove <ID_набора> <слово или фраза>"); return
        try: set_id = int(parts[1])
        except ValueError:
            await message.answer("ID набора должен быть числом."); return
        async with session_factory() as session:
            if await get_set(session, message.chat.id, set_id) is None:
                await message.answer("Набор не найден."); return
            row = (await session.execute(select(FilterItem).where(FilterItem.filter_set_id == set_id, FilterItem.value == parts[2].strip()))).scalar_one_or_none()
            if row is None:
                await message.answer("Элемент не найден."); return
            await session.delete(row); await session.commit()
        await message.answer("✅ Элемент удалён.")

    @router.message(Command("filterset_action"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_action(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer("Формат: /filterset_action <ID> <delete|warning|mute|ban> [секунды_для_mute]"); return
        try: set_id = int(parts[1])
        except ValueError:
            await message.answer("ID набора должен быть числом."); return
        action = parts[2].lower()
        if action not in {"delete", "warning", "mute", "ban"}:
            await message.answer("Действие: delete, warning, mute или ban."); return
        mute_seconds = None
        if action == "mute":
            if len(parts) != 4:
                await message.answer("Для mute укажи срок в секундах: /filterset_action <ID> mute <секунды>"); return
            try: mute_seconds = int(parts[3])
            except ValueError: mute_seconds = 0
            if mute_seconds <= 0:
                await message.answer("Срок мута должен быть больше нуля."); return
        async with session_factory() as session:
            filter_set = await get_set(session, message.chat.id, set_id)
            if filter_set is None: await message.answer("Набор не найден."); return
            filter_set.action = action; filter_set.mute_seconds = mute_seconds; await session.commit()
        await message.answer(f"✅ Набор #{set_id}: действие {action}" + (f", {mute_seconds} сек." if action == "mute" else ""))

    @router.message(Command("filterset_delete"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_delete_message(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
            await message.answer("Формат: /filterset_delete <ID> <on|off>"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            filter_set = await get_set(session, message.chat.id, set_id)
            if filter_set is None: await message.answer("Набор не найден."); return
            filter_set.delete_message = parts[2].lower() == "on"; await session.commit()
        await message.answer(f"✅ Удаление сообщения для #{set_id}: {parts[2].lower()}")

    @router.message(Command("filterset_enabled"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_enabled(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
            await message.answer("Формат: /filterset_enabled <ID> <on|off>"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.is_active = parts[2].lower() == "on"; await session.commit()
        await message.answer(f"✅ Набор #{set_id}: {'включён' if parts[2].lower() == 'on' else 'выключен'}.")

    @router.message(Command("filterset_match"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_match(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"whole", "contains"}:
            await message.answer("Формат: /filterset_match <ID> <whole|contains>"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.match_type = parts[2].lower(); await session.commit()
        await message.answer(f"✅ Режим совпадения #{set_id}: {parts[2].lower()}")

    @router.message(Command("filterset_case"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_case(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
            await message.answer("Формат: /filterset_case <ID> <on|off>"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.case_sensitive = parts[2].lower() == "on"; await session.commit()
        await message.answer(f"✅ Учёт регистра #{set_id}: {parts[2].lower()}")

    @router.message(Command("filterset_admins"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_admins(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"exclude", "include"}:
            await message.answer("Формат: /filterset_admins <ID> <exclude|include>"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.exclude_admins = parts[2].lower() == "exclude"; await session.commit()
        await message.answer(f"✅ Администраторы для #{set_id}: {parts[2].lower()}")

    @router.message(Command("filterset_priority"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_priority(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3: await message.answer("Формат: /filterset_priority <ID> <число>"); return
        try: set_id, priority = int(parts[1]), int(parts[2])
        except ValueError: await message.answer("ID и приоритет должны быть числами."); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.priority = priority; await session.commit()
        await message.answer(f"✅ Приоритет набора #{set_id}: {priority}")

    @router.message(Command("filterset_reason"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_reason(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3: await message.answer("Формат: /filterset_reason <ID> <причина>"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.reason = parts[2][:255]; await session.commit()
        await message.answer("✅ Причина сохранена.")

    @router.message(Command("filterset_remove"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_remove(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2] != "CONFIRM":
            await message.answer("Удаление необратимо. Формат: /filterset_remove <ID> CONFIRM"); return
        try: set_id = int(parts[1])
        except ValueError: return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            await session.delete(row); await session.commit()
        await message.answer(f"🗑 Набор #{set_id} удалён.")

    @router.message(Command("whitelist_add"), F.chat.type.in_({"group", "supergroup"}))
    async def whitelist_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None or target.is_bot: await message.answer("Ответь /whitelist_add на сообщение пользователя."); return
        async with session_factory() as session:
            exists = (await session.execute(select(ModerationWhitelist).where(ModerationWhitelist.chat_id == message.chat.id, ModerationWhitelist.user_id == target.id))).scalar_one_or_none()
            if not exists: session.add(ModerationWhitelist(chat_id=message.chat.id, user_id=target.id)); await session.commit()
        await message.answer(f"✅ {target.full_name} добавлен в whitelist этой группы.")

    @router.message(Command("whitelist_remove"), F.chat.type.in_({"group", "supergroup"}))
    async def whitelist_remove(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None: await message.answer("Ответь /whitelist_remove на сообщение пользователя."); return
        async with session_factory() as session:
            row = (await session.execute(select(ModerationWhitelist).where(ModerationWhitelist.chat_id == message.chat.id, ModerationWhitelist.user_id == target.id))).scalar_one_or_none()
            if row: await session.delete(row); await session.commit()
        await message.answer(f"✅ {target.full_name} удалён из whitelist.")

    @router.message(Command("warnings"), F.chat.type.in_({"group", "supergroup"}))
    async def warnings(message: Message) -> None:
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        if target is None: return
        async with session_factory() as session:
            count = (await session.execute(select(func.count(ModerationWarning.id)).where(ModerationWarning.chat_id == message.chat.id, ModerationWarning.user_id == target.id))).scalar_one()
        await message.answer(f"⚠️ Предупреждения {target.full_name}: {count}")

    @router.message(Command("filtersets"), F.chat.type.in_({"group", "supergroup"}))
    async def filtersets(message: Message) -> None:
        async with session_factory() as session:
            rows = (await session.execute(select(FilterSet).where(FilterSet.chat_id == message.chat.id).order_by(FilterSet.id))).scalars().all()
        if not rows: await message.answer("Наборов фильтра пока нет."); return
        lines = ["🛡 Наборы фильтра:"]
        for row in rows:
            mute = f"/{row.mute_seconds}s" if row.action == "mute" and row.mute_seconds else ""
            lines.append(f"#{row.id} {'✅' if row.is_active else '❌'} {row.name} [{row.kind}] → {row.action}{mute}; delete={'on' if row.delete_message else 'off'}; match={row.match_type}; case={'on' if row.case_sensitive else 'off'}; admins={'exclude' if row.exclude_admins else 'include'}; p={row.priority}")
        await message.answer("\n".join(lines))

    @router.message(Command("modlog"), F.chat.type.in_({"group", "supergroup"}))
    async def modlog(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        async with session_factory() as session:
            rows = (await session.execute(select(ModerationAction).where(ModerationAction.chat_id == message.chat.id).order_by(ModerationAction.created_at.desc()).limit(10))).scalars().all()
        if not rows: await message.answer("Журнал модерации пока пуст."); return
        lines = ["📋 Последние срабатывания:"]
        for row in rows: lines.append(f"#{row.id} user={row.user_id} set={row.filter_set_id} match={row.matched_value!r} final={row.action} ok={row.telegram_ok}")
        await message.answer("\n".join(lines))

    return router
