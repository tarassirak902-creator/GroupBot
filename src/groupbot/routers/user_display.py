from html import escape

from groupbot.models import User


def clickable_identity(
    *,
    telegram_user_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
) -> str:
    """Render a Telegram identity as one clickable display name.

    Prefer the user's Telegram first/last name. Use @username only when no name is
    available. The numeric Telegram id is used only inside the tg:// link.
    """
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    label = full_name or (f"@{username}" if username else "Пользователь")
    href = f"tg://user?id={telegram_user_id}"
    return f'<a href="{href}">{escape(label)}</a>'


def clickable_user_display(user: User) -> str:
    return clickable_identity(
        telegram_user_id=user.telegram_user_id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
    )
