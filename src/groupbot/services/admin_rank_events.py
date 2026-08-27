from __future__ import annotations

from html import escape

from groupbot.models import AdminRole, User
from groupbot.routers.admin_hierarchy import RANK_META
from groupbot.routers.user_display import clickable_identity, clickable_user_display

HELPER_ROLE = "Помощник"


def actor_link(actor) -> str:
    return clickable_identity(
        telegram_user_id=actor.id,
        first_name=actor.first_name,
        last_name=actor.last_name,
        username=actor.username,
    )


def _relation(old_role: AdminRole, new_role: AdminRole) -> str:
    old_meta = RANK_META.get(old_role.name)
    new_meta = RANK_META.get(new_role.name)
    if old_meta is None or new_meta is None:
        return "transfer"
    old_level = old_meta[2]
    new_level = new_meta[2]
    if new_level < old_level:
        return "promotion"
    if new_level > old_level:
        return "demotion"
    return "transfer"


def assignment_event(user: User, new_role: AdminRole, actor, old_role: AdminRole | None = None) -> str:
    target = clickable_user_display(user)
    by = actor_link(actor)
    new_name = escape(new_role.name)
    if old_role is None:
        if new_role.name == HELPER_ROLE:
            return f"🔹 {target} назначен «<b>{new_name}</b>» — наставник {by}."
        return f"👑 {target} назначен «<b>{new_name}</b>» — назначил {by}."
    if old_role.id == new_role.id:
        if new_role.name == HELPER_ROLE:
            return f"🔹 {target} уже назначен «<b>{new_name}</b>» — наставник {by}."
        return f"👑 {target} уже назначен «<b>{new_name}</b>» — подтвердил {by}."
    old_name = escape(old_role.name)
    relation = _relation(old_role, new_role)
    if relation == "promotion":
        return f"⬆️ {target} повышен с «<b>{old_name}</b>» до «<b>{new_name}</b>» — повысил {by}."
    if relation == "demotion":
        if new_role.name == HELPER_ROLE:
            return f"🔹 {target} переведён с «<b>{old_name}</b>» в «<b>{new_name}</b>» — наставник {by}."
        return f"⬇️ {target} понижен с «<b>{old_name}</b>» до «<b>{new_name}</b>» — понизил {by}."
    return f"🔄 {target} переведён с «<b>{old_name}</b>» на «<b>{new_name}</b>» — перевёл {by}."


def removal_event(user: User | None, target_id: int, actor) -> str:
    target = clickable_user_display(user) if user is not None else f'<a href="tg://user?id={target_id}">{target_id}</a>'
    return f"🚫 {target} снят с администрации — снял {actor_link(actor)}."
