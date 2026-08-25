from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def post_editor_keyboard(deal_id: int, *, has_photo: bool, has_button: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"ads:post:text:{deal_id}"),
            InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"ads:post:photo:{deal_id}"),
        ],
        [InlineKeyboardButton(text="🔘 Изменить кнопку", callback_data=f"ads:post:button:{deal_id}")],
    ]
    if has_photo or has_button:
        extra: list[InlineKeyboardButton] = []
        if has_photo:
            extra.append(InlineKeyboardButton(text="🗑 Фото", callback_data=f"ads:post:remove_photo:{deal_id}"))
        if has_button:
            extra.append(InlineKeyboardButton(text="🗑 Кнопку", callback_data=f"ads:post:remove_button:{deal_id}"))
        rows.append(extra)
    rows.append([
        InlineKeyboardButton(text="✅ Подтвердить и отправить", callback_data=f"ads:post:submit2:{deal_id}")
    ])
    rows.append([
        InlineKeyboardButton(text="❌ Отменить рекламный пост", callback_data=f"ads:post:cancel:{deal_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
