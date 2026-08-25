from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import (
    AdminAssignment,
    AdminPermission,
    AdminRole,
    Group,
    GroupSettings,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from groupbot.routers.manual_moderation import (
    DEFAULT_WARNING_LIMIT,
    MAX_WARNING_LIMIT,
    MIN_WARNING_LIMIT,
    _warning_stage,
)
from groupbot.services.audit import write_audit
from groupbot.services.permissions import is_group_owner
from groupbot.services.subscriptions import active_subscription_for_owner, effective_limit_for_owner
from groupbot.services.users import upsert_user


KNOWN_PERMISSIONS = [
    ("warning", "⚠️ Предупреждение"),
    ("mute", "🔇 Мут"),
    ("ban", "⛔ Бан"),
    ("unmute", "🔊 Размут"),
    ("unban", "✅ Разбан"),
    ("delete", "🗑 Удаление сообщений"),
    ("pin", "📌 Закрепление сообщений"),
    ("stats", "📊 Полная статистика"),
]
WARNING_LIMIT_CHOICES = (3, 4, 5, 6, 7, 8, 9, 10, 15, 20)
STANDARD_ADMIN_ROLE_NAMES = frozenset({
    "Зам. владельца",
    "Глав. админ",
    "Администратор чата",
    "Администратор войса",
    "Помощник",
})


class AdminRoleState(StatesGroup):
    waiting_name = State()


def _back_group(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _moderation_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Бан / Мут / Пред", callback_data=f"gctl:mod_help:{chat_id}")],
            [InlineKeyboardButton(text="📋 Банлист / Мутлист / Преды", callback_data=f"gctl:punish_lists:{chat_id}")],
            [InlineKeyboardButton(text="⚖️ Причины наказаний", callback_data=f"gctl:reasons:{chat_id}")],
            [InlineKeyboardButton(text="🎚 Режим админ-команд", callback_data=f"gctl:mode:{chat_id}")],
            [InlineKeyboardButton(text="📈 Шкала предупреждений", callback_data=f"gctl:warnings:{chat_id}")],
            [
                InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data=f"gctl:feature:{chat_id}:words"),
                InlineKeyboardButton(text="📝 Запрещённые фразы", callback_data=f"gctl:feature:{chat_id}:phrases"),
            ],
            [
                InlineKeyboardButton(text="💬 Антифлуд", callback_data=f"gctl:feature:{chat_id}:antiflood"),
                InlineKeyboardButton(text="🔁 Антиспам", callback_data=f"gctl:feature:{chat_id}:antispam"),
            ],
            [
                InlineKeyboardButton(text="🔗 Антиссылки", callback_data=f"gctl:feature:{chat_id}:antilinks"),
                InlineKeyboardButton(text="✅ Белый список", callback_data=f"gctl:feature:{chat_id}:whitelist"),
            ],
            [
                InlineKeyboardButton(text="🧩 Капча", callback_data=f"gctl:feature:{chat_id}:captcha"),
                InlineKeyboardButton(text="🚨 Антирейд", callback_data=f"gctl:feature:{chat_id}:antiraid"),
            ],
            [InlineKeyboardButton(text="🕐 Расписание защиты", callback_data=f"gctl:feature:{chat_id}:protection_schedule")],
            [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
        ]
    )


def _administration_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👑 Ранги администрации", callback_data=f"gctl:roles:{chat_id}")],
            [InlineKeyboardButton(text="👮 Администраторы", callback_data=f"gctl:admins:{chat_id}")],
            [InlineKeyboardButton(text="🛡 Права рангов", callback_data=f"gctl:roles:{chat_id}")],
            [InlineKeyboardButton(text="🧯 Резервный администратор", callback_data=f"gctl:reserve:{chat_id}")],
            [InlineKeyboardButton(text="🌐 Сетевые администраторы", callback_data=f"gctl:network_admins:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
        ]
    )


def _mode_keyboard(chat_id: int, current: str) -> InlineKeyboardMarkup:
    def label(value: str, text: str) -> str:
        return ("✅ " if current == value else "▫️ ") + text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("text", "Текстовый"), callback_data=f"gctl:setmode:{chat_id}:text")],
            [InlineKeyboardButton(text=label("buttons", "Кнопки"), callback_data=f"gctl:setmode:{chat_id}:buttons")],
            [InlineKeyboardButton(text=label("both", "Оба режима"), callback_data=f"gctl:setmode:{chat_id}:both")],
            [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
        ]
    )


def _warning_keyboard(chat_id: int, current: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    values = list(WARNING_LIMIT_CHOICES)
    for start in range(0, len(values), 4):
        row: list[InlineKeyboardButton] = []
        for value in values[start:start + 4]:
            prefix = "✅ " if value == current else ""
            row.append(InlineKeyboardButton(text=f"{prefix}{value}", callback_data=f"gctl:setwarnings:{chat_id}:{value}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _warning_scale_text(limit: int) -> str:
    lines = [
        "📈 <b>Шкала предупреждений</b>",
        "",
        f"Лимит предупреждений: <b>{limit}</b>",
        "",
    ]
    for count in range(1, limit + 1):
        stage = _warning_stage(count, limit)
        if stage == "ban":
            action = "⛔ Бан"
        elif stage == "mute_1h":
            action = "🔇 Мут 1 час"
        elif stage == "mute_15m":
            action = "🔇 Мут 15 минут"
        else:
            action = "⚠️ Предупреждение"
        lines.append(f"{count}/{limit} — {action}")
    lines.extend([
        "",
        "Лимит применяется к ручным предупреждениям и автоматической модерации этой группы.",
    ])
    return "\n".join(lines)


def _roles_keyboard(chat_id: int, roles: list[AdminRole]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for role in roles:
        icon = "✅" if role.is_active else "⛔"
        rows.append([InlineKeyboardButton(text=f"{icon} {role.name}"[:64], callback_data=f"gctl:role:{chat_id}:{role.id}")])
    rows.append([InlineKeyboardButton(text="➕ Создать ранг", callback_data=f"gctl:role_create:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _role_keyboard(chat_id: int, role: AdminRole, permissions: dict[str, bool]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, title in KNOWN_PERMISSIONS:
        icon = "✅" if permissions.get(key, False) else "❌"
        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {title}",
                callback_data=f"gctl:perm:{chat_id}:{role.id}:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"gctl:perm_save:{chat_id}:{role.id}")])
    rows.append([
        InlineKeyboardButton(
            text="⛔ Выключить ранг" if role.is_active else "✅ Включить ранг",
            callback_data=f"gctl:role_toggle:{chat_id}:{role.id}",
        )
    ])
    if role.name not in STANDARD_ADMIN_ROLE_NAMES:
        rows.append([InlineKeyboardButton(text="🗑 Удалить ранг", callback_data=f"gctl:role_delete:{chat_id}:{role.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Все ранги", callback_data=f"gctl:roles:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ensure_group_settings(session: AsyncSession, chat_id: int) -> GroupSettings:
    row = (
        await session.execute(select(GroupSettings).where(GroupSettings.chat_id == chat_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        row = GroupSettings(chat_id=chat_id, moderation_config={})
        session.add(row)
        await session.flush()
    return row


async def _owner_access(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    if not await is_group_owner(session, chat_id, user_id):
        return False
    return await active_subscription_for_owner(session, user_id) is not None


async def _rank_limit(session: AsyncSession, owner_id: int) -> int | None:
    return await effective_limit_for_owner(session, owner_id, "admin_ranks")


async def _custom_rank_count(session: AsyncSession, chat_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AdminRole)
            .where(
                AdminRole.chat_id == chat_id,
                ~AdminRole.name.in_(STANDARD_ADMIN_ROLE_NAMES),
            )
        )
    ).scalar_one()


def create_group_control_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_control")

    @router.callback_query(F.data.startswith("group:section:"))
    async def section(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            return
        section_key = parts[3]
        if section_key not in {"moderation", "administration"}:
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return
            group = (
                await session.execute(select(Group).where(Group.chat_id == chat_id))
            ).scalar_one_or_none()
            settings = await _ensure_group_settings(session, chat_id)
            moderation_config = settings.moderation_config or {}
            roles_count = await _custom_rank_count(session, chat_id)
            assignments_count = (
                await session.execute(select(func.count()).select_from(AdminAssignment).where(AdminAssignment.chat_id == chat_id))
            ).scalar_one()

        if callback.message is None:
            return
        title = group.title if group and group.title else str(chat_id)
        if section_key == "moderation":
            mode = moderation_config.get("admin_command_mode", "both")
            mode_name = {"text": "Текстовый", "buttons": "Кнопки", "both": "Оба режима"}.get(mode, "Оба режима")
            await callback.message.edit_text(
                "🛡 <b>Модерация</b>\n\n"
                f"Группа: <b>{title}</b>\n"
                f"Режим админ-команд: <b>{mode_name}</b>\n\n"
                "Здесь настраиваются ручные наказания, причины, предупреждения и защитные модули группы.",
                parse_mode="HTML",
                reply_markup=_moderation_keyboard(chat_id),
            )
        else:
            await callback.message.edit_text(
                "👮 <b>Администрация</b>\n\n"
                f"Группа: <b>{title}</b>\n"
                f"Собственных рангов: <b>{roles_count}</b>\n"
                f"Назначений в Mimorus: <b>{assignments_count}</b>\n\n"
                "Владелец может создавать собственные ранги и отдельно задавать доступные действия.",
                parse_mode="HTML",
                reply_markup=_administration_keyboard(chat_id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:content_filters:"))
    async def content_filters(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except (ValueError, IndexError):
            return
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
        if callback.message is not None:
            await callback.message.edit_text(
                "🚫 <b>Запрещённые слова/фразы</b>\n\n"
                "Выберите список, который хотите посмотреть или изменить.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data=f"gctl:feature:{chat_id}:words")],
                        [InlineKeyboardButton(text="📝 Запрещённые фразы", callback_data=f"gctl:feature:{chat_id}:phrases")],
                        [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")],
                    ]
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:mode:"))
    async def mode_screen(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            current = (settings.moderation_config or {}).get("admin_command_mode", "both")
        if callback.message is not None:
            await callback.message.edit_text(
                "🎚 <b>Режим админ-команд</b>\n\n"
                "Текстовый — действие и причина пишутся ответом на сообщение.\n"
                "Кнопки — после команды бот предлагает срок/причину.\n"
                "Оба режима — работают оба варианта.",
                parse_mode="HTML",
                reply_markup=_mode_keyboard(chat_id, current),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:setmode:"))
    async def set_mode(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        chat_id = int(parts[2])
        mode = parts[3]
        if mode not in {"text", "buttons", "both"}:
            await callback.answer("Некорректный режим.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                config = dict(settings.moderation_config or {})
                config["admin_command_mode"] = mode
                settings.moderation_config = config
                await write_audit(
                    session,
                    "group.moderation_mode_changed",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="group",
                    target_id=str(chat_id),
                    payload={"mode": mode},
                )
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ Режим админ-команд обновлён.",
                reply_markup=_mode_keyboard(chat_id, mode),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("gctl:warnings:"))
    async def warning_scale(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            settings = await _ensure_group_settings(session, chat_id)
            try:
                current = int((settings.moderation_config or {}).get("warning_limit", DEFAULT_WARNING_LIMIT))
            except (TypeError, ValueError):
                current = DEFAULT_WARNING_LIMIT
            current = max(MIN_WARNING_LIMIT, min(MAX_WARNING_LIMIT, current))
        if callback.message is not None:
            await callback.message.edit_text(
                _warning_scale_text(current),
                parse_mode="HTML",
                reply_markup=_warning_keyboard(chat_id, current),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:setwarnings:"))
    async def set_warning_limit(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        try:
            chat_id = int(parts[2])
            limit = int(parts[3])
        except (ValueError, IndexError):
            return
        if limit not in WARNING_LIMIT_CHOICES:
            await callback.answer("Некорректный лимит.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                settings = await _ensure_group_settings(session, chat_id)
                config = dict(settings.moderation_config or {})
                config["warning_limit"] = limit
                settings.moderation_config = config
                await write_audit(
                    session,
                    "group.warning_limit_changed",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="group",
                    target_id=str(chat_id),
                    payload={"warning_limit": limit},
                )
        if callback.message is not None:
            await callback.message.edit_text(
                _warning_scale_text(limit),
                parse_mode="HTML",
                reply_markup=_warning_keyboard(chat_id, limit),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("gctl:mod_help:"))
    async def mod_help(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        if callback.message is not None:
            await callback.message.edit_text(
                "⚠️ <b>Бан / Мут / Пред</b>\n\n"
                "Подтверждённые команды ответом на сообщение пользователя:\n"
                "<code>пред</code>\n"
                "<code>мут</code>\n"
                "<code>бан</code>\n"
                "<code>разбан</code>\n"
                "<code>размут</code>\n\n"
                "В текстовом режиме после действия можно указать причину. В режиме кнопок Mimorus должен предложить срок/причину.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")]
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:punish_lists:"))
    async def punish_lists(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        if callback.message is not None:
            await callback.message.edit_text(
                "📋 <b>Списки наказаний</b>\n\n"
                "Подтверждённые команды:\n"
                "• <code>мои баны</code> — выданные этим администратором;\n"
                "• <code>мои муты</code>;\n"
                "• <code>выдал пред</code>;\n"
                "• <code>банлист</code> — все баны группы + кто выдал;\n"
                "• <code>мутлист</code>;\n"
                "• <code>преды</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")]
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:reasons:"))
    async def reasons(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        if callback.message is not None:
            await callback.message.edit_text(
                "⚖️ <b>Причины наказаний</b>\n\n"
                "По MASTER-ТЗ причины создаются отдельно для каждой группы и могут быть связаны с действием и фиксированным сроком.\n\n"
                "Примеры из ТЗ:\n"
                "• Флуд → мут 30 минут\n"
                "• Оскорбление → мут 2 часа\n"
                "• Спам → бан\n\n"
                "Редактор причин будет следующим подблоком модерации.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")]
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:feature:"))
    async def protection_feature(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        chat_id = int(parts[2])
        key = parts[3]
        titles = {
            "words": "🚫 Запрещённые слова",
            "phrases": "📝 Запрещённые фразы",
            "antiflood": "💬 Антифлуд",
            "antispam": "🔁 Антиспам",
            "antilinks": "🔗 Антиссылки",
            "whitelist": "✅ Белый список",
            "captcha": "🧩 Капча",
            "antiraid": "🚨 Антирейд",
            "protection_schedule": "🕐 Расписание защиты",
        }
        if callback.message is not None:
            await callback.message.edit_text(
                f"{titles.get(key, '🛡 Защита')}\n\n"
                "Раздел включён в структуру модерации по MASTER-ТЗ. Его рабочие параметры будут подключаться без добавления неутверждённых значений.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Модерация", callback_data=f"group:section:{chat_id}:moderation")]
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:roles:"))
    async def roles(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            rows = (
                await session.execute(select(AdminRole).where(AdminRole.chat_id == chat_id).order_by(AdminRole.id))
            ).scalars().all()
            limit = await _rank_limit(session, callback.from_user.id)
        custom_count = sum(1 for role in rows if role.name not in STANDARD_ADMIN_ROLE_NAMES)
        suffix = f"\nЛимит тарифа с дополнениями: <b>{limit}</b> ранга." if limit is not None else ""
        if callback.message is not None:
            await callback.message.edit_text(
                "👑 <b>Ранги администрации</b>\n\n"
                f"Собственных рангов: <b>{custom_count}</b>.{suffix}\n"
                "Новые ранги создаются без автоматически выданных прав: владелец включает каждое действие сам.",
                parse_mode="HTML",
                reply_markup=_roles_keyboard(chat_id, list(rows)),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:role_create:"))
    async def role_create(callback: CallbackQuery, state: FSMContext) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            limit = await _rank_limit(session, callback.from_user.id)
            count = await _custom_rank_count(session, chat_id)
        if limit is not None and count >= limit:
            await callback.answer(f"Достигнут лимит дополнительных административных рангов: {limit}.", show_alert=True)
            return
        await state.set_state(AdminRoleState.waiting_name)
        await state.update_data(chat_id=chat_id)
        if callback.message is not None:
            prompt = await callback.message.answer(
                "Отправьте название нового дополнительного административного ранга (1–128 символов)."
            )
            await state.update_data(prompt_message_id=prompt.message_id)
        await callback.answer()

    @router.message(AdminRoleState.waiting_name, F.chat.type == "private")
    async def role_name(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 128:
            await message.answer("Название должно быть длиной 1–128 символов.")
            return
        data = await state.get_data()
        chat_id = int(data["chat_id"])
        prompt_message_id = data.get("prompt_message_id")
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, message.from_user.id):
                    await state.clear()
                    await message.answer("Недостаточно прав.")
                    return
                limit = await _rank_limit(session, message.from_user.id)
                count = await _custom_rank_count(session, chat_id)
                if limit is not None and count >= limit:
                    await state.clear()
                    await message.answer(f"Достигнут лимит дополнительных административных рангов: {limit}.")
                    return
                exists = (
                    await session.execute(select(AdminRole.id).where(AdminRole.chat_id == chat_id, AdminRole.name == name))
                ).scalar_one_or_none()
                if exists is not None:
                    await message.answer("Ранг с таким названием уже существует.")
                    return
                role = AdminRole(chat_id=chat_id, name=name, is_active=True)
                session.add(role)
                await session.flush()
                role_id = role.id
                for key, _ in KNOWN_PERMISSIONS:
                    session.add(AdminPermission(role_id=role_id, permission=key, allowed=False))
                await write_audit(
                    session,
                    "group.admin_role_created",
                    chat_id=chat_id,
                    actor_user_id=message.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"name": name},
                )

        permissions = {key: False for key, _ in KNOWN_PERMISSIONS}
        await state.clear()
        await state.update_data(
            permission_draft_chat_id=chat_id,
            permission_draft_role_id=role_id,
            permission_draft=permissions,
        )
        try:
            await message.delete()
        except Exception:
            pass

        text = (
            "👑 <b>Настройка админ-ранга</b>\n\n"
            f"Название: <b>{name}</b>\n"
            "Статус: ✅ включён\n"
            "Назначено пользователей: <b>0</b>\n\n"
            "✅ Ранг создан. Выберите нужные разрешения и нажмите <b>💾 Сохранить</b>."
        )
        keyboard = _role_keyboard(chat_id, role, permissions)
        if prompt_message_id is not None:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=int(prompt_message_id),
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("gctl:role:"))
    async def role_card(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        chat_id = int(parts[2])
        role_id = int(parts[3])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
            role = (
                await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id))
            ).scalar_one_or_none()
            perm_rows = (
                await session.execute(select(AdminPermission).where(AdminPermission.role_id == role_id))
            ).scalars().all()
            assignments = (
                await session.execute(select(func.count()).select_from(AdminAssignment).where(AdminAssignment.role_id == role_id))
            ).scalar_one()
        if role is None:
            await callback.answer("Ранг не найден.", show_alert=True)
            return
        permissions = {row.permission: row.allowed for row in perm_rows}
        if callback.message is not None:
            await callback.message.edit_text(
                "👑 <b>Админ-ранг</b>\n\n"
                f"Название: <b>{role.name}</b>\n"
                f"Статус: {'✅ включён' if role.is_active else '⛔ выключен'}\n"
                f"Назначено пользователей: <b>{assignments}</b>\n\n"
                "Права включаются владельцем индивидуально:",
                parse_mode="HTML",
                reply_markup=_role_keyboard(chat_id, role, permissions),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:perm:"))
    async def toggle_permission(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 4)
        chat_id = int(parts[2])
        role_id = int(parts[3])
        permission = parts[4]
        if permission not in {key for key, _ in KNOWN_PERMISSIONS}:
            await callback.answer("Неизвестное право.", show_alert=True)
            return
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id).with_for_update())
                ).scalar_one_or_none()
                if role is None:
                    await callback.answer("Ранг не найден.", show_alert=True)
                    return
                row = (
                    await session.execute(
                        select(AdminPermission).where(
                            AdminPermission.role_id == role_id,
                            AdminPermission.permission == permission,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = AdminPermission(role_id=role_id, permission=permission, allowed=True)
                    session.add(row)
                else:
                    row.allowed = not row.allowed
                new_value = row.allowed
                await write_audit(
                    session,
                    "group.admin_permission_changed",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"permission": permission, "allowed": new_value},
                )
        callback.data = f"gctl:role:{chat_id}:{role_id}"
        await role_card(callback)

    @router.callback_query(F.data.startswith("gctl:role_toggle:"))
    async def role_toggle(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        chat_id = int(parts[2])
        role_id = int(parts[3])
        async with session_factory() as session:
            async with session.begin():
                if not await _owner_access(session, chat_id, callback.from_user.id):
                    await callback.answer("Недостаточно прав.", show_alert=True)
                    return
                role = (
                    await session.execute(select(AdminRole).where(AdminRole.id == role_id, AdminRole.chat_id == chat_id).with_for_update())
                ).scalar_one_or_none()
                if role is None:
                    await callback.answer("Ранг не найден.", show_alert=True)
                    return
                role.is_active = not role.is_active
                await write_audit(
                    session,
                    "group.admin_role_toggled",
                    chat_id=chat_id,
                    actor_user_id=callback.from_user.id,
                    target_type="admin_role",
                    target_id=str(role_id),
                    payload={"is_active": role.is_active},
                )
        callback.data = f"gctl:role:{chat_id}:{role_id}"
        await role_card(callback)

    @router.callback_query(F.data.startswith("gctl:admins:"))
    async def admins(callback: CallbackQuery, bot: Bot) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Недостаточно прав.", show_alert=True)
                return
        try:
            telegram_admins = await bot.get_chat_administrators(chat_id)
        except Exception:
            await callback.answer("Не удалось получить список администраторов Telegram.", show_alert=True)
            return
        lines = ["👮 <b>Администраторы</b>", "", f"Telegram-администраторов: <b>{len(telegram_admins)}</b>", ""]
        for member in telegram_admins[:30]:
            user = member.user
            name = user.full_name or (f"@{user.username}" if user.username else str(user.id))
            role_name = "владелец" if member.status == "creator" else "администратор"
            lines.append(f"• {name} — {role_name}")
        if callback.message is not None:
            await callback.message.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")]
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:reserve:"))
    async def reserve(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        async with session_factory() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(AdminAssignment).where(
                        AdminAssignment.chat_id == chat_id,
                        AdminAssignment.is_reserve.is_(True),
                    )
                )
            ).scalar_one()
        if callback.message is not None:
            await callback.message.edit_text(
                "🧯 <b>Резервный администратор</b>\n\n"
                f"Назначено в Mimorus: <b>{count}</b>\n\n"
                "MASTER-ТЗ предусматривает резервного администратора. Назначение конкретного пользователя подключим вместе с общим экраном назначения рангов.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")]
                ]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("gctl:network_admins:"))
    async def network_admins(callback: CallbackQuery) -> None:
        chat_id = int((callback.data or "").split(":", 2)[2])
        if callback.message is not None:
            await callback.message.edit_text(
                "🌐 <b>Сетевые администраторы</b>\n\n"
                "Сетевые администраторы относятся только к группам одной сетки того же владельца. Глобальные права на чужие группы не выдаются.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Администрация", callback_data=f"group:section:{chat_id}:administration")]
                ]),
            )
        await callback.answer()

    return router
