from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import FilterItem, FilterSet, ModerationAction, ModerationWarning, ModerationWhitelist


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
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

    async def parse_set_id(message: Message, position: int = 1) -> int | None:
        parts = (message.text or "").split()
        if len(parts) <= position:
            return None
        try:
            return int(parts[position])
        except ValueError:
            return None

    @router.message(Command("modhelp"), F.chat.type.in_({"group", "supergroup"}))
    async def modhelp(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        await message.answer(
            "🛡 Управление автомодерацией\n\n"
            "/filtersets — список наборов\n"
            "/filterset <ID> — подробности и элементы\n"
            "/filterset_add <word|phrase> <название>\n"
            "/filteritem_add <ID> <слово/фраза>\n"
            "/filteritem_remove <ID> <слово/фраза>\n"
            "/filterset_action <ID> <delete|warning|mute|ban> [сек]\n"
            "/filterset_delete <ID> <on|off>\n"
            "/filterset_enabled <ID> <on|off>\n"
            "/filterset_match <ID> <whole|contains>\n"
            "/filterset_case <ID> <on|off>\n"
            "/filterset_admins <ID> <exclude|include>\n"
            "/filterset_whitelist <ID> <exclude|include>\n"
            "/filterset_priority <ID> <число>\n"
            "/filterset_reason <ID> <причина>\n"
            "/filterset_remove <ID> CONFIRM\n\n"
            "/whitelist — список whitelist\n"
            "/whitelist_add — ответом на пользователя\n"
            "/whitelist_remove — ответом на пользователя\n"
            "/warnings — свои предупреждения или ответом на пользователя\n"
            "/warnings_clear — ответом на пользователя\n"
            "/modlog [количество 1..30] — журнал"
        )

    @router.message(Command("filterset_add"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3 or parts[1].lower() not in {"word", "phrase"}:
            await message.answer("Формат: /filterset_add <word|phrase> <название>"); return
        kind, name = parts[1].lower(), parts[2].strip()
        if not name or len(name) > 128:
            await message.answer("Название должно содержать от 1 до 128 символов."); return
        async with session_factory() as session:
            if (await session.execute(select(FilterSet).where(FilterSet.chat_id == message.chat.id, FilterSet.name == name))).scalar_one_or_none():
                await message.answer("Набор с таким названием уже существует."); return
            row = FilterSet(chat_id=message.chat.id, name=name, kind=kind, match_type="whole" if kind == "word" else "contains", action="delete", delete_message=True, exclude_admins=True, exclude_whitelist=True)
            session.add(row); await session.commit(); await session.refresh(row)
        await message.answer(f"🛡 Создан набор #{row.id}: {name} ({kind}), действие: delete.")

    @router.message(Command("filteritem_add"), F.chat.type.in_({"group", "supergroup"}))
    async def filteritem_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Формат: /filteritem_add <ID_набора> <слово или фраза>"); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        value = parts[2].strip()
        if not value or len(value) > 500 or "\n" in value or "\r" in value:
            await message.answer("Элемент должен быть одной строкой длиной 1–500 символов."); return
        async with session_factory() as session:
            if await get_set(session, message.chat.id, set_id) is None:
                await message.answer("Набор не найден в этой группе."); return
            exists = (await session.execute(select(FilterItem).where(FilterItem.filter_set_id == set_id, FilterItem.value == value))).scalar_one_or_none()
            if exists: await message.answer("Такой элемент уже есть в наборе."); return
            session.add(FilterItem(filter_set_id=set_id, value=value)); await session.commit()
        await message.answer(f"✅ Добавлено в набор #{set_id}: {value}")

    @router.message(Command("filteritem_remove"), F.chat.type.in_({"group", "supergroup"}))
    async def filteritem_remove(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Формат: /filteritem_remove <ID_набора> <слово или фраза>"); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        async with session_factory() as session:
            if await get_set(session, message.chat.id, set_id) is None: await message.answer("Набор не найден."); return
            row = (await session.execute(select(FilterItem).where(FilterItem.filter_set_id == set_id, FilterItem.value == parts[2].strip()))).scalar_one_or_none()
            if row is None: await message.answer("Элемент не найден."); return
            await session.delete(row); await session.commit()
        await message.answer("✅ Элемент удалён.")

    @router.message(Command("filterset_action"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_action(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer("Формат: /filterset_action <ID> <delete|warning|mute|ban> [секунды_для_mute]"); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        action = parts[2].lower()
        if action not in {"delete", "warning", "mute", "ban"}:
            await message.answer("Действие: delete, warning, mute или ban."); return
        mute_seconds = None
        if action == "mute":
            if len(parts) != 4:
                await message.answer("Для mute: /filterset_action <ID> mute <секунды>"); return
            try: mute_seconds = int(parts[3])
            except ValueError: mute_seconds = 0
            if not 1 <= mute_seconds <= 31_536_000:
                await message.answer("Срок мута: от 1 секунды до 365 дней."); return
        elif len(parts) != 3:
            await message.answer("Для этого действия дополнительный параметр не нужен."); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.action, row.mute_seconds = action, mute_seconds; await session.commit()
        await message.answer(f"✅ Набор #{set_id}: действие {action}" + (f", {mute_seconds} сек." if mute_seconds else ""))

    async def set_toggle(message: Message, bot: Bot, field: str, usage: str, labels: tuple[str, str]) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
            await message.answer(usage); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        value = parts[2].lower() == "on"
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            setattr(row, field, value); await session.commit()
        await message.answer(f"✅ Набор #{set_id}: {labels[0] if value else labels[1]}.")

    @router.message(Command("filterset_delete"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_delete_message(message: Message, bot: Bot) -> None:
        await set_toggle(message, bot, "delete_message", "Формат: /filterset_delete <ID> <on|off>", ("удаление on", "удаление off"))

    @router.message(Command("filterset_enabled"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_enabled(message: Message, bot: Bot) -> None:
        await set_toggle(message, bot, "is_active", "Формат: /filterset_enabled <ID> <on|off>", ("включён", "выключен"))

    @router.message(Command("filterset_case"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_case(message: Message, bot: Bot) -> None:
        await set_toggle(message, bot, "case_sensitive", "Формат: /filterset_case <ID> <on|off>", ("регистр учитывается", "регистр не учитывается"))

    @router.message(Command("filterset_match"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_match(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"whole", "contains"}:
            await message.answer("Формат: /filterset_match <ID> <whole|contains>"); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.match_type = parts[2].lower(); await session.commit()
        await message.answer(f"✅ Режим совпадения #{set_id}: {parts[2].lower()}")

    async def set_exclusion(message: Message, bot: Bot, field: str, command: str, subject: str) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in {"exclude", "include"}:
            await message.answer(f"Формат: /{command} <ID> <exclude|include>"); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            setattr(row, field, parts[2].lower() == "exclude"); await session.commit()
        await message.answer(f"✅ {subject} для #{set_id}: {parts[2].lower()}")

    @router.message(Command("filterset_admins"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_admins(message: Message, bot: Bot) -> None:
        await set_exclusion(message, bot, "exclude_admins", "filterset_admins", "Администраторы")

    @router.message(Command("filterset_whitelist"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_whitelist(message: Message, bot: Bot) -> None:
        await set_exclusion(message, bot, "exclude_whitelist", "filterset_whitelist", "Whitelist")

    @router.message(Command("filterset_priority"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_priority(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3: await message.answer("Формат: /filterset_priority <ID> <число -1000..1000>"); return
        try: set_id, priority = int(parts[1]), int(parts[2])
        except ValueError: await message.answer("ID и приоритет должны быть числами."); return
        if not -1000 <= priority <= 1000: await message.answer("Приоритет должен быть от -1000 до 1000."); return
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
        except ValueError: await message.answer("ID набора должен быть числом."); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            row.reason = parts[2].strip()[:255] or None; await session.commit()
        await message.answer("✅ Причина сохранена.")

    @router.message(Command("filterset_remove"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_remove(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2] != "CONFIRM":
            await message.answer("Удаление необратимо. Формат: /filterset_remove <ID> CONFIRM"); return
        try: set_id = int(parts[1])
        except ValueError: await message.answer("ID набора должен быть числом."); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            await session.delete(row); await session.commit()
        await message.answer(f"🗑 Набор #{set_id} удалён.")

    @router.message(Command("filterset"), F.chat.type.in_({"group", "supergroup"}))
    async def filterset_details(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        set_id = await parse_set_id(message)
        if set_id is None: await message.answer("Формат: /filterset <ID>"); return
        async with session_factory() as session:
            row = await get_set(session, message.chat.id, set_id)
            if row is None: await message.answer("Набор не найден."); return
            items = (await session.execute(select(FilterItem).where(FilterItem.filter_set_id == set_id).order_by(FilterItem.id))).scalars().all()
        mute = f"/{row.mute_seconds}s" if row.action == "mute" and row.mute_seconds else ""
        values = "\n".join(f"• {item.value}" for item in items[:50]) or "• элементов нет"
        extra = f"\n… ещё {len(items)-50}" if len(items) > 50 else ""
        await message.answer(
            f"🛡 Набор #{row.id}: {row.name}\n"
            f"Статус: {'on' if row.is_active else 'off'} | тип: {row.kind}\n"
            f"Совпадение: {row.match_type} | регистр: {'on' if row.case_sensitive else 'off'}\n"
            f"Действие: {row.action}{mute} | delete: {'on' if row.delete_message else 'off'} | priority: {row.priority}\n"
            f"Админы: {'exclude' if row.exclude_admins else 'include'} | whitelist: {'exclude' if row.exclude_whitelist else 'include'}\n"
            f"Причина: {row.reason or '—'}\n"
            f"Элементы ({len(items)}):\n{values}{extra}"
        )

    @router.message(Command("whitelist_add"), F.chat.type.in_({"group", "supergroup"}))
    async def whitelist_add(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None or target.is_bot: await message.answer("Ответь /whitelist_add на сообщение пользователя."); return
        async with session_factory() as session:
            exists = (await session.execute(select(ModerationWhitelist).where(ModerationWhitelist.chat_id == message.chat.id, ModerationWhitelist.user_id == target.id))).scalar_one_or_none()
            if exists: await message.answer(f"ℹ️ {target.full_name} уже в whitelist."); return
            session.add(ModerationWhitelist(chat_id=message.chat.id, user_id=target.id)); await session.commit()
        await message.answer(f"✅ {target.full_name} добавлен в whitelist этой группы.")

    @router.message(Command("whitelist_remove"), F.chat.type.in_({"group", "supergroup"}))
    async def whitelist_remove(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None: await message.answer("Ответь /whitelist_remove на сообщение пользователя."); return
        async with session_factory() as session:
            row = (await session.execute(select(ModerationWhitelist).where(ModerationWhitelist.chat_id == message.chat.id, ModerationWhitelist.user_id == target.id))).scalar_one_or_none()
            if row is None: await message.answer(f"ℹ️ {target.full_name} не находится в whitelist."); return
            await session.delete(row); await session.commit()
        await message.answer(f"✅ {target.full_name} удалён из whitelist.")

    @router.message(Command("whitelist"), F.chat.type.in_({"group", "supergroup"}))
    async def whitelist(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        async with session_factory() as session:
            rows = (await session.execute(select(ModerationWhitelist).where(ModerationWhitelist.chat_id == message.chat.id).order_by(ModerationWhitelist.id))).scalars().all()
        if not rows: await message.answer("Whitelist этой группы пуст."); return
        await message.answer("🛡 Whitelist:\n" + "\n".join(f"• user_id={row.user_id}" for row in rows))

    @router.message(Command("warnings"), F.chat.type.in_({"group", "supergroup"}))
    async def warnings(message: Message) -> None:
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        if target is None: return
        async with session_factory() as session:
            count = (await session.execute(select(func.count(ModerationWarning.id)).where(ModerationWarning.chat_id == message.chat.id, ModerationWarning.user_id == target.id))).scalar_one()
        await message.answer(f"⚠️ Предупреждения {target.full_name}: {count}")

    @router.message(Command("warnings_clear"), F.chat.type.in_({"group", "supergroup"}))
    async def warnings_clear(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None: await message.answer("Ответь /warnings_clear на сообщение пользователя."); return
        async with session_factory() as session:
            result = await session.execute(delete(ModerationWarning).where(ModerationWarning.chat_id == message.chat.id, ModerationWarning.user_id == target.id))
            await session.commit()
        await message.answer(f"✅ Предупреждения {target.full_name} очищены ({result.rowcount or 0}).")

    @router.message(Command("filtersets"), F.chat.type.in_({"group", "supergroup"}))
    async def filtersets(message: Message) -> None:
        async with session_factory() as session:
            rows = (await session.execute(select(FilterSet).where(FilterSet.chat_id == message.chat.id).order_by(FilterSet.id))).scalars().all()
        if not rows: await message.answer("Наборов фильтра пока нет."); return
        lines = ["🛡 Наборы фильтра:"]
        for row in rows:
            mute = f"/{row.mute_seconds}s" if row.action == "mute" and row.mute_seconds else ""
            lines.append(f"#{row.id} {'✅' if row.is_active else '❌'} {row.name} [{row.kind}] → {row.action}{mute}; delete={'on' if row.delete_message else 'off'}; match={row.match_type}; case={'on' if row.case_sensitive else 'off'}; admins={'exclude' if row.exclude_admins else 'include'}; white={'exclude' if row.exclude_whitelist else 'include'}; p={row.priority}")
        await message.answer("\n".join(lines))

    @router.message(Command("modlog"), F.chat.type.in_({"group", "supergroup"}))
    async def modlog(message: Message, bot: Bot) -> None:
        if not await require_admin(message, bot): return
        parts = (message.text or "").split()
        limit = 10
        if len(parts) > 2: await message.answer("Формат: /modlog [количество 1..30]"); return
        if len(parts) == 2:
            try: limit = int(parts[1])
            except ValueError: await message.answer("Количество должно быть числом."); return
            if not 1 <= limit <= 30: await message.answer("Количество должно быть от 1 до 30."); return
        async with session_factory() as session:
            rows = (await session.execute(select(ModerationAction).where(ModerationAction.chat_id == message.chat.id).order_by(ModerationAction.created_at.desc()).limit(limit))).scalars().all()
        if not rows: await message.answer("Журнал модерации пока пуст."); return
        lines = ["📋 Последние срабатывания:"]
        for row in rows:
            status = "ok" if row.telegram_ok is True else "error" if row.telegram_ok is False else "n/a"
            lines.append(f"#{row.id} user={row.user_id} set={row.filter_set_id} match={row.matched_value!r} final={row.action} {status}")
        await message.answer("\n".join(lines))

    return router
