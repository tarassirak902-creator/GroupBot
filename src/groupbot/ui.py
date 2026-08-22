from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def private_main_menu(*, is_creator: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👥 Мои группы"), KeyboardButton(text="🌐 Сетки групп")],
        [KeyboardButton(text="📢 Реклама"), KeyboardButton(text="💳 Тариф и подписка")],
        [KeyboardButton(text="🛠 Поддержка"), KeyboardButton(text="👤 Мой аккаунт")],
    ]
    if is_creator:
        rows.append([KeyboardButton(text="👑 Панель создателя")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
