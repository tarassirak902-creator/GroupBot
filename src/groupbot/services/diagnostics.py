from aiogram import Bot


def _mark(value: bool | None) -> str:
    return "🟢" if bool(value) else "🔴"


async def rights_diagnostic(bot: Bot, chat_id: int) -> tuple[str, bool]:
    me = await bot.get_me()
    member = await bot.get_chat_member(chat_id, me.id)
    is_admin = member.status == "administrator"
    delete_messages = is_admin and bool(getattr(member, "can_delete_messages", False))
    restrict_members = is_admin and bool(getattr(member, "can_restrict_members", False))
    pin_messages = is_admin and bool(getattr(member, "can_pin_messages", False))
    invite_users = is_admin and bool(getattr(member, "can_invite_users", False))
    promote_members = is_admin and bool(getattr(member, "can_promote_members", False))
    post_messages = is_admin and getattr(member, "can_post_messages", None) is not False
    receive_events = is_admin
    text = (
        "🔎 Диагностика\n"
        f"{_mark(delete_messages)} Удаление сообщений\n"
        f"{_mark(restrict_members)} Бан пользователей\n"
        f"{_mark(restrict_members)} Ограничение пользователей\n"
        f"{_mark(pin_messages)} Закрепление сообщений\n"
        f"{_mark(invite_users)} Приглашение пользователей / рекламные ссылки\n"
        f"{_mark(promote_members)} Назначение администраторов\n"
        f"{_mark(post_messages)} Публикация сообщений\n"
        f"{_mark(receive_events)} Получение событий"
    )
    critical_ok = delete_messages and restrict_members and invite_users and promote_members
    return text, critical_ok
