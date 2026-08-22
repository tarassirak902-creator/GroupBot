from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def private_main_menu(*, is_creator: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👥 Мои группы"), KeyboardButton(text="🌐 Сетки групп")],
        [KeyboardButton(text="📢 Реклама"), KeyboardButton(text="💳 Тариф и подписка")],
        [KeyboardButton(text="🛠 Поддержка"), KeyboardButton(text="👤 Мой аккаунт")],
    ]
    if is_creator:
        rows.append([KeyboardButton(text="👑 Панель создателя")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def owned_groups_keyboard(groups: list[tuple[int, str | None, str]]) -> InlineKeyboardMarkup:
    icons = {"active": "✅", "pending": "⏳", "disabled": "⚠️", "left": "❌"}
    rows = []
    for chat_id, title, status in groups:
        label = f"{icons.get(status, '•')} {title or chat_id}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"group:open:{chat_id}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_management_keyboard(chat_id: int, *, active: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛡 Модерация", callback_data=f"group:section:{chat_id}:moderation")],
        [InlineKeyboardButton(text="👮 Администрация", callback_data=f"group:section:{chat_id}:administration")],
        [InlineKeyboardButton(text="🤖 Автоматизация", callback_data=f"group:section:{chat_id}:automation")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"group:section:{chat_id}:statistics")],
        [InlineKeyboardButton(text="🎮 Настройки развлечений", callback_data=f"group:section:{chat_id}:games")],
        [InlineKeyboardButton(text="📢 Реклама группы", callback_data=f"group:section:{chat_id}:advertising")],
        [InlineKeyboardButton(text="⚙️ Настройки группы", callback_data=f"group:section:{chat_id}:settings")],
        [InlineKeyboardButton(text="🔎 Диагностика", callback_data=f"group:diagnostic:{chat_id}")],
    ]
    if active:
        rows.append([InlineKeyboardButton(text="⚠️ Отключить группу", callback_data=f"group:disable:{chat_id}")])
    else:
        rows.append([InlineKeyboardButton(text="ℹ️ Как подключить", callback_data=f"group:reconnect:{chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="group:list"), InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
