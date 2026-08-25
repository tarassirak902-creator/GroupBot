from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.models import Subscription, SubscriptionStatus, Tariff
from groupbot.payment_models import TelegramStarsPayment
from groupbot.services.audit import write_audit
from groupbot.services.users import upsert_user
from groupbot.ui import tariff_card_keyboard


TARIFF_ICONS = {
    "TEST": "🎁",
    "BASIC": "🔹",
    "STANDARD": "🔷",
    "PRO": "💎",
    "MAX": "👑",
}


def _stars_price(tariff: Tariff) -> int | None:
    config = tariff.limits_json or {}
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


def _payload(owner_user_id: int, tariff: Tariff, stars: int) -> str:
    return f"sub:{owner_user_id}:{tariff.id}:{tariff.code}:{stars}"


def _parse_payload(payload: str) -> tuple[int, int, str, int] | None:
    parts = payload.split(":")
    if len(parts) != 5 or parts[0] != "sub":
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3].upper(), int(parts[4])
    except (TypeError, ValueError):
        return None


def _limit(tariff: Tariff, key: str, fallback: str = "—") -> str:
    value = (tariff.limits_json or {}).get(key)
    if value is None:
        return fallback
    return str(value)


def _tariff_card_text(tariff: Tariff) -> str:
    icon = TARIFF_ICONS.get(tariff.code, "💳")
    members = (
        f"{tariff.max_members_per_group:,}".replace(",", " ")
        if tariff.max_members_per_group is not None
        else "до технического максимума Telegram"
    )
    duration = f"{tariff.duration_days} дней" if tariff.duration_days else "не настроен"

    if tariff.code == "TEST":
        return (
            f"{icon} <b>TEST — 3 дня</b>\n\n"
            "Пробный тариф нужен для проверки возможностей Mimorus на реальной группе любого размера.\n\n"
            f"👤 Участников в группе: <b>до {members}</b>\n"
            "👥 Основная группа: <b>1</b>\n"
            "🧪 Доп. группа: <b>+1 только для теста сетки</b>\n"
            f"🌐 Сетей: <b>{_limit(tariff, 'networks')}</b>\n"
            f"🔗 Групп в одной сети: <b>{_limit(tariff, 'network_groups_per_network')}</b>\n"
            f"🚫 Списков запрещённых слов: <b>{_limit(tariff, 'blocked_word_lists')}</b>\n"
            f"🔤 Запрещённых слов всего: <b>{_limit(tariff, 'blocked_words')}</b>\n"
            f"📝 Списков запрещённых фраз: <b>{_limit(tariff, 'blocked_phrase_lists')}</b>\n"
            f"💬 Запрещённых фраз всего: <b>{_limit(tariff, 'blocked_phrases')}</b>\n"
            f"⚖️ Своих причин: <b>{_limit(tariff, 'custom_reasons')}</b>\n"
            f"👑 Доп. админ-рангов: <b>{_limit(tariff, 'admin_ranks')}</b>\n"
            f"📤 Экспортов статистики: <b>{_limit(tariff, 'exports')} за TEST</b>\n"
            "💎 VIP: <b>без лимита</b>\n"
            f"🛡 Расписаний защиты: <b>{_limit(tariff, 'protection_schedules')}</b>\n\n"
            "⭐ Цена: <b>бесплатно</b>"
        )

    stars = _stars_price(tariff)
    price = f"{stars} ⭐" if stars is not None else "не настроена"
    exports = _limit(tariff, "exports", "без лимита")
    return (
        f"{icon} <b>{tariff.name}</b>\n\n"
        f"👤 Участников в одной группе: <b>до {members}</b>\n"
        f"👥 Подключённых групп: <b>{tariff.max_groups or '—'}</b>\n"
        f"🌐 Сетей: <b>{_limit(tariff, 'networks')}</b>\n"
        f"🔗 Групп в одной сети: <b>{_limit(tariff, 'network_groups_per_network')}</b>\n\n"
        f"🚫 Списков запрещённых слов: <b>{_limit(tariff, 'blocked_word_lists')}</b>\n"
        f"🔤 Запрещённых слов всего: <b>{_limit(tariff, 'blocked_words')}</b>\n"
        f"📝 Списков запрещённых фраз: <b>{_limit(tariff, 'blocked_phrase_lists')}</b>\n"
        f"💬 Запрещённых фраз всего: <b>{_limit(tariff, 'blocked_phrases')}</b>\n"
        f"⚖️ Своих причин: <b>{_limit(tariff, 'custom_reasons')}</b>\n"
        f"👑 Доп. админ-рангов: <b>{_limit(tariff, 'admin_ranks')}</b>\n"
        f"👮 Резервных администраторов: <b>{_limit(tariff, 'reserve_admins')}</b>\n"
        f"📤 Экспортов статистики: <b>{exports}{'/мес' if exports != 'без лимита' else ''}</b>\n"
        "💎 VIP: <b>без лимита</b>\n"
        f"🛡 Расписаний защиты: <b>{_limit(tariff, 'protection_schedules')}</b>\n\n"
        f"⏳ Срок: <b>{duration}</b>\n"
        f"⭐ Цена: <b>{price}</b>\n\n"
        "Оплата платных тарифов производится через Telegram Stars."
    )


def create_subscription_payments_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="subscription_payments")

    @router.callback_query(F.data.startswith("tariff:card:"))
    async def tariff_card(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        code = (callback.data or "").split(":", 2)[2].upper()
        async with session_factory() as session:
            tariff = (
                await session.execute(
                    select(Tariff).where(Tariff.code == code, Tariff.is_active.is_(True))
                )
            ).scalar_one_or_none()
            active = (
                await session.execute(
                    select(Subscription.id).where(
                        Subscription.owner_user_id == callback.from_user.id,
                        Subscription.status == SubscriptionStatus.active.value,
                        Subscription.ends_at > datetime.now(timezone.utc),
                    ).limit(1)
                )
            ).scalar_one_or_none()
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
        can_activate_test = code == "TEST" and active is None and previous_trial is None
        await callback.message.edit_text(
            _tariff_card_text(tariff),
            parse_mode="HTML",
            reply_markup=tariff_card_keyboard(code, can_activate_test=can_activate_test),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tariff:choose:"))
    async def choose_paid_tariff(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        code = (callback.data or "").split(":", 2)[2].upper()
        async with session_factory() as session:
            tariff = (
                await session.execute(
                    select(Tariff).where(Tariff.code == code, Tariff.is_active.is_(True))
                )
            ).scalar_one_or_none()
        if tariff is None or tariff.is_trial or code == "TEST":
            await callback.answer("Этот тариф нельзя оплатить через Stars.", show_alert=True)
            return
        stars = _stars_price(tariff)
        if stars is None:
            await callback.answer(
                "Цена тарифа в Telegram Stars ещё не настроена создателем.",
                show_alert=True,
            )
            return
        if tariff.duration_days is None or tariff.duration_days <= 0:
            await callback.answer("Для тарифа не настроен срок действия.", show_alert=True)
            return

        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"Mimorus — {tariff.name}",
            description=f"Подписка Mimorus на {tariff.duration_days} дн.",
            payload=_payload(callback.from_user.id, tariff, stars),
            currency="XTR",
            prices=[LabeledPrice(label=tariff.name, amount=stars)],
            provider_token="",
        )
        await callback.answer()

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        parsed = _parse_payload(query.invoice_payload)
        if parsed is None:
            await query.answer(ok=False, error_message="Некорректный платёж Mimorus.")
            return
        owner_user_id, tariff_id, code, invoice_stars = parsed
        if owner_user_id != query.from_user.id or query.currency != "XTR":
            await query.answer(ok=False, error_message="Платёж не соответствует вашему аккаунту.")
            return

        async with session_factory() as session:
            tariff = (
                await session.execute(
                    select(Tariff).where(
                        Tariff.id == tariff_id,
                        Tariff.code == code,
                        Tariff.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if tariff is None or tariff.is_trial:
            await query.answer(ok=False, error_message="Тариф сейчас недоступен.")
            return
        current_stars = _stars_price(tariff)
        if (
            current_stars is None
            or current_stars != invoice_stars
            or query.total_amount != invoice_stars
        ):
            await query.answer(
                ok=False,
                error_message="Цена тарифа изменилась. Откройте тариф и создайте новый счёт.",
            )
            return
        if tariff.duration_days is None or tariff.duration_days <= 0:
            await query.answer(ok=False, error_message="Для тарифа не настроен срок действия.")
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def successful_payment(message: Message) -> None:
        if message.from_user is None or message.successful_payment is None:
            return
        payment = message.successful_payment
        parsed = _parse_payload(payment.invoice_payload)
        if parsed is None or payment.currency != "XTR":
            return
        owner_user_id, tariff_id, code, invoice_stars = parsed
        if owner_user_id != message.from_user.id or payment.total_amount != invoice_stars:
            return

        already_processed = False
        tariff_name = code
        ends_at: datetime | None = None
        async with session_factory() as session:
            async with session.begin():
                await upsert_user(session, message.from_user)
                existing = (
                    await session.execute(
                        select(TelegramStarsPayment.id).where(
                            TelegramStarsPayment.telegram_payment_charge_id
                            == payment.telegram_payment_charge_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    already_processed = True
                else:
                    tariff = (
                        await session.execute(
                            select(Tariff).where(Tariff.id == tariff_id, Tariff.code == code).with_for_update()
                        )
                    ).scalar_one_or_none()
                    if tariff is None or tariff.is_trial:
                        return
                    current_stars = _stars_price(tariff)
                    if current_stars != invoice_stars or tariff.duration_days is None or tariff.duration_days <= 0:
                        return

                    now = datetime.now(timezone.utc)
                    active_rows = (
                        await session.execute(
                            select(Subscription).where(
                                Subscription.owner_user_id == owner_user_id,
                                Subscription.status == SubscriptionStatus.active.value,
                                Subscription.ends_at > now,
                            ).with_for_update()
                        )
                    ).scalars().all()
                    for row in active_rows:
                        row.status = SubscriptionStatus.cancelled.value

                    subscription = Subscription(
                        owner_user_id=owner_user_id,
                        tariff_id=tariff.id,
                        status=SubscriptionStatus.active.value,
                        started_at=now,
                        ends_at=now + timedelta(days=tariff.duration_days),
                        is_trial=False,
                    )
                    session.add(subscription)
                    await session.flush()

                    session.add(
                        TelegramStarsPayment(
                            telegram_payment_charge_id=payment.telegram_payment_charge_id,
                            provider_payment_charge_id=payment.provider_payment_charge_id or None,
                            owner_user_id=owner_user_id,
                            tariff_id=tariff.id,
                            subscription_id=subscription.id,
                            invoice_payload=payment.invoice_payload,
                            stars_amount=payment.total_amount,
                            currency=payment.currency,
                        )
                    )
                    await write_audit(
                        session,
                        "subscription.stars_paid",
                        actor_user_id=owner_user_id,
                        target_type="subscription",
                        target_id=str(subscription.id),
                        payload={
                            "tariff": tariff.code,
                            "stars": payment.total_amount,
                            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
                            "ends_at": subscription.ends_at.isoformat(),
                        },
                    )
                    tariff_name = tariff.name
                    ends_at = subscription.ends_at

        if already_processed:
            await message.answer("✅ Этот платёж уже обработан. Подписка Mimorus активна.")
            return
        if ends_at is None:
            return
        ends_text = ends_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        await message.answer(
            "✅ <b>Оплата Telegram Stars прошла успешно!</b>\n\n"
            f"Тариф: <b>{tariff_name}</b>\n"
            f"Оплачено: <b>{invoice_stars} ⭐</b>\n"
            f"Подписка действует до: <b>{ends_text}</b>",
            parse_mode="HTML",
        )

    return router
