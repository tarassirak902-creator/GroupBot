from html import escape

from groupbot.models import User


def clickable_user_display(user: User) -> str:
    """Render human-friendly Telegram identity with a plain separator.

    Name and username are separate profile links. The ` | ` separator stays
    ordinary text, while the Telegram user id remains the internal identity key.
    """
    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()
    username = f"@{user.username}" if user.username else ""
    href = f"tg://user?id={user.telegram_user_id}"

    if full_name and username:
        return (
            f'<a href="{href}">{escape(full_name)}</a>'
            " | "
            f'<a href="{href}">{escape(username)}</a>'
        )
    if full_name:
        return f'<a href="{href}">{escape(full_name)}</a>'
    if username:
        return f'<a href="{href}">{escape(username)}</a>'
    return f'<a href="{href}">Пользователь</a>'
