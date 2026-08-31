from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Subscription, Tariff
from groupbot.routers.advertising import _advertising_keyboard
from groupbot.routers.group_control import _owner_access
from groupbot.services.subscriptions import active_subscription_for_owner
from groupbot.ui import tariff_back_keyboard, tariff_card_keyboard


HANDLED_SECTIONS = {"automation", "games", "advertising", "settings"}
TARIFF_ICONS = {
    "TEST": "🎁",
    "BASIC": "🔹",
    "STANDARD": "🔷",
    "PRO": "💎",
    "MAX": "👑",
}


def _back_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Управление группой", callback_data=f"group:open:{chat_id}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
    ])


def _limit(config: dict, key: str) -> str:
    value = config.get(key)
    return "без отдельного лимита" if value is None else str(value)


def _stars_price(tariff: Tariff) -> int | None:
    config = dict(tariff.limits_json or {})
    raw = config.get("stars_price")
    if raw is None:
        raw = config.get("price_label")
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith("⭐"):
        text = text[:-1].strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _truthful_tariff_text(tariff: Tariff) -> str:
    limits = dict(tariff.limits_json or {})
    icon = TARIFF_ICONS.get(tariff.code, "💳")
    groups = tariff.max_groups if tariff.max_groups is not None else "—"
    duration = f"{tariff.duration_days} дней" if tariff.duration_days else "не настроен"
    price = "бесплатно" if tariff.code == "TEST" else (
        f"{_stars_price(tariff)} ⭐" if _stars_price(tariff) is not None else "не настроена"
    )
    title = "TEST — 3 дня" if tariff.code == "TEST" else tariff.name
    return (
        f"{icon} <b>{title}</b>\n\n"
        "<b>Работает сейчас:</b>\n"
        f"👥 Подключённых групп: <b>{groups}</b>\n"
        f"🌐 Сеток: <b>{_limit(limits, 'networks')}</b>\n"
        f"🏠 Групп в одной сетке: <b>{_limit(limits, 'network_groups_per_network')}</b>\n"
        f"🚫 Списков запрещённых слов: <b>{_limit(limits, 'blocked_word_lists')}</b>\n"
        f"🔤 Запрещённых слов всего: <b>{_limit(limits, 'blocked_words')}</b>\n"
        f"📝 Списков запрещённых фраз: <b>{_limit(limits, 'blocked_phrase_lists')}</b>\n"
        f"💬 Запрещённых фраз всего: <b>{_limit(limits, 'blocked_phrases')}</b>\n"
        f"⚖️ Своих причин наказаний: <b>{_limit(limits, 'custom_reasons')}</b>\n"
        f"👑 Дополнительных админ-рангов: <b>{_limit(limits, 'admin_ranks')}</b>\n"
        f"👮 Резервных администраторов: <b>{_limit(limits, 'reserve_admins')}</b>\n"
        f"🕐 Расписаний защиты: <b>{_limit(limits, 'protection_schedules')}</b>\n\n"
        "Рабочие модули модерации, входной защиты, статистики, администрации и сетевой модерации доступны в пределах тарифа.\n\n"
        "<b>Ещё не реализовано как готовая функция:</b>\n"
        "автосообщения, автоповторы, шаблоны, собственные достижения, автоматические роли, лог-группы и экспорт статистики.\n\n"
        f"⏳ Срок тарифа: <b>{duration}</b>\n"
        f"⭐ Цена: <b>{price}</b>"
    )


def create_group_sections_nav_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="group_sections_nav")

    @router.callback_query(
        F.data.regexp(r"^group:section:-?\d+:(automation|games|advertising|settings)$")
    )
    async def section(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректный раздел.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        section_key = parts[3]
        if section_key not in HANDLED_SECTIONS:
            return

        async with session_factory() as session:
            if not await _owner_access(session, chat_id, callback.from_user.id):
                await callback.answer("Нужны права владельца и активный тариф.", show_alert=True)
                return

        if callback.message is None:
            await callback.answer()
            return

        if section_key == "advertising":
            await callback.message.edit_text(
                "🟣 <b>Mimorus · Реклама</b>\n\n"
                "Покупайте рекламные размещения или выставляйте свою подключённую группу как площадку.",
                parse_mode="HTML",
                reply_markup=_advertising_keyboard(),
            )
        elif section_key == "automation":
            await callback.message.edit_text(
                "🤖 <b>Автоматизация — скоро</b>\n\n"
                "Автосообщения, повторы, напоминания и другие сценарии группы ещё не подключены как готовые функции.\n\n"
                "Настройки здесь пока не сохраняются и скрытых действий Mimorus не выполняет.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(chat_id),
            )
        elif section_key == "games":
            await callback.message.edit_text(
                "🎮 <b>Развлечения — скоро</b>\n\n"
                "Настройки игр, RP, отношений, заданий и рейтингов ещё не подключены к текущей версии.\n\n"
                "Этот экран информационный и ничего в группе не меняет.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(chat_id),
            )
        else:
            await callback.message.edit_text(
                "⚙️ <b>Дополнительные настройки — скоро</b>\n\n"
                "Рабочие настройки уже находятся в разделах модерации, администрации, статистики, рекламы и диагностики.\n\n"
                "Этот дополнительный раздел пока информационный и ничего не сохраняет.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(chat_id),
            )
        await callback.answer()

    @router.message(
        F.chat.type == "private",
        F.text.in_({"🛠 Поддержка", "🛠 Поддержка (скоро)"}),
    )
    async def support_stub(message: Message) -> None:
        await message.answer(
            "🛠 <b>Поддержка — скоро</b>\n\n"
            "Встроенная система обращений в поддержку пока не подключена. "
            "Кнопка информационная: тикет не создаётся и сообщение никуда автоматически не отправляется.",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("tariff:card:"))
    async def truthful_tariff_card(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        code = (callback.data or "").split(":", 2)[2].upper()
        async with session_factory() as session:
            tariff = (
                await session.execute(
                    select(Tariff).where(Tariff.code == code, Tariff.is_active.is_(True))
                )
            ).scalar_one_or_none()
            active = await active_subscription_for_owner(session, callback.from_user.id)
            previous_trial = None
            if code == "TEST":
                previous_trial = (
                    await session.execute(
                        select(Subscription.id).where(
                            Subscription.owner_user_id == callback.from_user.id,
                            Subscription.is_trial.is_(True),
                        ).limit(1)
                    )
                ).scalar_one_or_none()
        if tariff is None:
            await callback.answer("Тариф сейчас недоступен.", show_alert=True)
            return
        await callback.message.edit_text(
            _truthful_tariff_text(tariff),
            parse_mode="HTML",
            reply_markup=tariff_card_keyboard(
                code,
                can_activate_test=code == "TEST" and active is None and previous_trial is None,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data == "tariff:addons")
    async def truthful_addons(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "📦 <b>Дополнительные покупки — пока недоступны</b>\n\n"
                "Каталог тарифов уже содержит технические ключи дополнительных лимитов, "
                "но отдельная покупка пакетов и их цены ещё не утверждены и не подключены.\n\n"
                "Этот экран информационный: нажатие ничего не покупает и не меняет подписку.",
                parse_mode="HTML",
                reply_markup=tariff_back_keyboard(),
            )
        await callback.answer()

    return router
