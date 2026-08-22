from html import escape

from groupbot.models import User


def clickable_identity(
    *,
    telegram_user_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
) -> str:
    """Render a public Telegram identity without exposing the numeric id.

    The numeric Telegram id is used only inside tg:// links and internal logic.
    Name and username are separate clickable links, while ` | ` stays plain text.
    """
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    username_label = f"@{username}" if username else ""
    href = f"tg://user?id={telegram_user_id}"

    if full_name and username_label:
        return (
            f'<a href="{href}">{escape(full_name)}</a>'
            " | "
            f'<a href="{href}">{escape(username_label)}</a>'
        )
    if full_name:
        return f'<a href="{href}">{escape(full_name)}</a>'
    if username_label:
        return f'<a href="{href}">{escape(username_label)}</a>'
    return f'<a href="{href}">Пользователь</a>'


def clickable_user_display(user: User) -> str:
    return clickable_identity(
        telegram_user_id=user.telegram_user_id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
    )
