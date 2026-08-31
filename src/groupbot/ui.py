from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def private_main_menu(*, is_creator: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👥 Мои группы"), KeyboardButton(text="🌐 Сетки групп")],
        [KeyboardButton(text="📢 Реклама"), KeyboardButton(text="💳 Тариф и подписка")],
        [KeyboardButton(text="🛠 Поддержка (скоро)"), KeyboardButton(text="👤 Мой аккаунт")],
    ]
    if is_creator:
        rows.append([KeyboardButton(text="👑 Панель создателя")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def tariff_center_keyboard(*, has_active: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎁 TEST — 3 дня", callback_data="tariff:card:TEST")],
        [InlineKeyboardButton(text="🔹 BASIC", callback_data="tariff:card:BASIC")],
        [InlineKeyboardButton(text="🔷 STANDARD", callback_data="tariff:card:STANDARD")],
        [InlineKeyboardButton(text="💎 PRO", callback_data="tariff:card:PRO")],
        [InlineKeyboardButton(text="👑 MAX", callback_data="tariff:card:MAX")],
        [InlineKeyboardButton(text="🛠 Собрать свой тариф", callback_data="tariff:custom")],
        [InlineKeyboardButton(text="📦 Дополнительные покупки", callback_data="tariff:addons")],
    ]
    if has_active:
        rows.append([InlineKeyboardButton(text="📜 Моя подписка", callback_data="tariff:subscription")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariff_card_keyboard(code: str, *, can_activate_test: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if can_activate_test:
        rows.append([InlineKeyboardButton(text="🎁 Активировать TEST на 3 дня", callback_data="tariff:activate_test")])
    elif code != "TEST":
        rows.append([InlineKeyboardButton(text="💳 Выбрать тариф", callback_data=f"tariff:choose:{code}")])
    rows.append([InlineKeyboardButton(text="◀️ Все тарифы", callback_data="tariff:show")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariff_activation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Активировать TEST на 3 дня", callback_data="tariff:activate_test")],
            [InlineKeyboardButton(text="💳 Все тарифы", callback_data="tariff:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def tariff_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Все тарифы", callback_data="tariff:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def owned_groups_keyboard(
    groups: list[tuple[int, str | None, str]],
    *,
    add_bot_url: str | None = None,
) -> InlineKeyboardMarkup:
    icons = {"active": "✅", "pending": "⏳", "disabled": "⚠️", "left": "❌"}
    rows = []
    if add_bot_url:
        rows.append([InlineKeyboardButton(text="➕ Добавить бота в группу", url=add_bot_url)])
    for chat_id, title, status in groups:
        label = f"{icons.get(status, '•')} {title or chat_id}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"group:open:{chat_id}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_locked_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Активировать TEST", callback_data="tariff:card:TEST")],
            [InlineKeyboardButton(text="💳 Тариф и подписка", callback_data="tariff:show")],
            [InlineKeyboardButton(text="🔎 Диагностика", callback_data=f"group:diagnostic:{chat_id}")],
            [InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"group:delete_prompt:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="group:list"), InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def group_management_keyboard(chat_id: int, *, active: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if active:
        rows.extend([
            [InlineKeyboardButton(text="🛡 Модерация", callback_data=f"group:section:{chat_id}:moderation")],
            [InlineKeyboardButton(text="👮 Администрация", callback_data=f"group:section:{chat_id}:administration")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data=f"group:section:{chat_id}:statistics")],
            [InlineKeyboardButton(text="📢 Реклама группы", callback_data=f"group:section:{chat_id}:advertising")],
            [InlineKeyboardButton(text="🤖 Автоматизация (скоро)", callback_data=f"group:section:{chat_id}:automation")],
            [InlineKeyboardButton(text="🎮 Развлечения (скоро)", callback_data=f"group:section:{chat_id}:games")],
            [InlineKeyboardButton(text="⚙️ Доп. настройки (скоро)", callback_data=f"group:section:{chat_id}:settings")],
            [InlineKeyboardButton(text="🔎 Диагностика", callback_data=f"group:diagnostic:{chat_id}")],
            [InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"group:delete_prompt:{chat_id}")],
        ])
    else:
        rows.extend([
            [InlineKeyboardButton(text="🔎 Диагностика подключения", callback_data=f"group:diagnostic:{chat_id}")],
            [InlineKeyboardButton(text="ℹ️ Как подключить", callback_data=f"group:reconnect:{chat_id}")],
            [InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"group:delete_prompt:{chat_id}")],
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="group:list"), InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
