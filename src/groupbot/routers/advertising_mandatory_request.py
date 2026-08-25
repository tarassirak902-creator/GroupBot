from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement

MANDATORY_DAILY_LIMIT = 3


class MandatoryRequestState(StatesGroup):
    waiting_target = State()


def _target_ref(text: str) -> str | None:
    value = text.strip()
    if value.startswith("https://t.me/"):
        value = value.split("https://t.me/", 1)[1].split("?", 1)[0].strip("/")
    elif value.startswith("http://t.me/"):
        value = value.split("http://t.me/", 1)[1].split("?", 1)[0].strip("/")
    value = value.removeprefix("@").strip()
    if not value or "/" in value or "+" in value:
        return None
    return "@" + value


def _seller_keyboard(deal_id: int, buyer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Связаться с покупателем", url=f"tg://user?id={buyer_id}")],
        [
            InlineKeyboardButton(text="✅ Одобрить ОП", callback_data=f"ads:mandatory:accept:{deal_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:mandatory:reject:{deal_id}"),
        ],
    ])


def create_advertising_mandatory_request_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_mandatory_request")

    @router.callback_query(F.data.regexp(r"^ads:req:type:\d+:mandatory$"))
    async def start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        listing_id = int((callback.data or "").split(":")[3])
        async with session_factory() as session:
            listing = (await session.execute(
                select(AdvertisingListing).where(
                    AdvertisingListing.id == listing_id,
                    AdvertisingListing.is_active.is_(True),
                    AdvertisingListing.offers_mandatory.is_(True),
                )
            )).scalar_one_or_none()
        if listing is None or listing.owner_user_id == callback.from_user.id:
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        await state.set_state(MandatoryRequestState.waiting_target)
        await state.update_data(mandatory_listing_id=listing_id)
        await callback.message.edit_text(
            "✅ <b>Покупка обязательной подписки</b>\n\n"
            "Сначала укажите группу или канал, на который должны подписываться участники площадки рекламодателя.\n\n"
            "Отправьте публичный <b>@username</b> или ссылку <b>https://t.me/...</b>.\n\n"
            "⚠️ Mimorus должен быть администратором в этой группе/канале, чтобы проверять подписку пользователей.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data="ads:buy")]
            ]),
        )
        await callback.answer()

    @router.message(MandatoryRequestState.waiting_target, F.chat.type == "private")
    async def target(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not message.text:
            return
        data = await state.get_data()
        listing_id = data.get("mandatory_listing_id")
        ref = _target_ref(message.text)
        try:
            await message.delete()
        except Exception:
            pass
        if not isinstance(listing_id, int) or ref is None:
            await message.answer("Укажите публичный @username или ссылку вида https://t.me/example.")
            return
        try:
            target_chat = await bot.get_chat(ref)
            bot_info = await bot.get_me()
            bot_member = await bot.get_chat_member(target_chat.id, bot_info.id)
            if bot_member.status not in {"administrator", "creator"}:
                raise RuntimeError("bot is not admin")
            member_count = await bot.get_chat_member_count(target_chat.id)
        except Exception:
            await message.answer(
                "Не удалось проверить эту группу/канал. Убедитесь, что ссылка публичная и Mimorus добавлен туда администратором."
            )
            return

        username = getattr(target_chat, "username", None)
        if not username:
            await message.answer("Для ОП сейчас нужна публичная группа/канал с @username.")
            return
        target_url = f"https://t.me/{username}"
        title = target_chat.title or f"@{username}"

        async with session_factory() as session:
            async with session.begin():
                listing = (await session.execute(
                    select(AdvertisingListing).where(
                        AdvertisingListing.id == listing_id,
                        AdvertisingListing.is_active.is_(True),
                        AdvertisingListing.offers_mandatory.is_(True),
                    ).with_for_update()
                )).scalar_one_or_none()
                if listing is None or listing.owner_user_id == message.from_user.id:
                    await state.clear()
                    await message.answer("Объявление больше недоступно.")
                    return
                existing = (await session.execute(
                    select(AdvertisingDeal.id).where(
                        AdvertisingDeal.listing_id == listing.id,
                        AdvertisingDeal.buyer_user_id == message.from_user.id,
                        AdvertisingDeal.status == "pending",
                        AdvertisingDeal.requested_mandatory.is_(True),
                    ).limit(1)
                )).scalar_one_or_none()
                if existing is not None:
                    await state.clear()
                    await message.answer("У вас уже есть заявка на ОП по этой площадке, ожидающая решения.")
                    return
                deal = AdvertisingDeal(
                    listing_id=listing.id,
                    seller_user_id=listing.owner_user_id,
                    buyer_user_id=message.from_user.id,
                    requested_post=False,
                    requested_mandatory=True,
                    status="pending",
                    agreed_terms_json={
                        "mandatory_price_stars": listing.mandatory_price_stars,
                        "mandatory_terms": listing.mandatory_terms_json,
                    },
                )
                session.add(deal)
                await session.flush()
                placement = AdvertisingPlacement(
                    deal_id=deal.id,
                    kind="mandatory",
                    status="ready",
                    config_json={
                        "price_stars": listing.mandatory_price_stars,
                        "terms": listing.mandatory_terms_json,
                        "target_chat_id": target_chat.id,
                        "target_title": title,
                        "target_username": username,
                        "target_url": target_url,
                        "target_member_count": member_count,
                    },
                )
                session.add(placement)
                deal_id = deal.id
                seller_id = deal.seller_user_id
                seller_group = listing.group_title_snapshot

        await state.clear()
        buyer_name = escape(message.from_user.full_name)
        seller_text = (
            "📥 <b>Новая заявка на ОП</b>\n\n"
            f"🏠 Ваша площадка: <b>{escape(seller_group)}</b>\n"
            f"👤 Покупатель: <b>{buyer_name}</b>\n\n"
            "🎯 <b>Куда вести обязательную подписку:</b>\n"
            f"🏠 <a href=\"{target_url}\">{escape(title)}</a>\n"
            f"🔗 @{escape(username)}\n"
            f"👥 Участников: <b>{member_count:,}</b>\n\n".replace(",", " ") +
            "После одобрения ОП включится автоматически в вашей группе."
        )
        try:
            await bot.send_message(
                seller_id,
                seller_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_seller_keyboard(deal_id, message.from_user.id),
            )
        except Exception:
            pass
        await message.answer(
            "✅ <b>Заявка на ОП отправлена рекламодателю</b>\n\n"
            f"🎯 Группа/канал: <a href=\"{target_url}\">{escape(title)}</a>\n"
            f"👥 Участников: <b>{member_count:,}</b>".replace(",", " "),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]),
        )

    @router.callback_query(F.data.regexp(r"^ads:mandatory:accept:\d+$"))
    async def accept(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        buyer_id = None
        target_title = ""
        target_url = ""
        seller_group = ""
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(
                    select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
                )).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                used = int((await session.execute(
                    select(func.count()).select_from(AdvertisingDeal).where(
                        AdvertisingDeal.listing_id == deal.listing_id,
                        AdvertisingDeal.accepted_at >= day_start,
                        AdvertisingDeal.requested_mandatory.is_(True),
                    )
                )).scalar_one())
                if used >= MANDATORY_DAILY_LIMIT:
                    await callback.answer("Лимит ОП на сегодня уже использован: 3 из 3.", show_alert=True)
                    return
                placement = (await session.execute(
                    select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "mandatory",
                    ).with_for_update()
                )).scalar_one_or_none()
                listing = (await session.execute(
                    select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id)
                )).scalar_one_or_none()
                if placement is None or placement.status != "ready" or listing is None:
                    await callback.answer("Данные ОП не готовы.", show_alert=True)
                    return
                placement.status = "active"
                placement.starts_at = now
                deal.status = "accepted"
                deal.accepted_at = now
                deal.started_at = now
                cfg = dict(placement.config_json or {})
                buyer_id = deal.buyer_user_id
                target_title = str(cfg.get("target_title") or "Группа")
                target_url = str(cfg.get("target_url") or "")
                seller_group = listing.group_title_snapshot

        if buyer_id is not None:
            try:
                await bot.send_message(
                    buyer_id,
                    "✅ <b>Рекламодатель одобрил ОП</b>\n\n"
                    f"🏠 Площадка: <b>{escape(seller_group)}</b>\n"
                    f"🎯 ОП на: <a href=\"{target_url}\">{escape(target_title)}</a>\n\n"
                    "🚀 Обязательная подписка включена автоматически.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        if callback.message is not None:
            await callback.message.edit_text(
                "✅ <b>ОП одобрена и запущена</b>\n\n"
                f"🎯 <a href=\"{target_url}\">{escape(target_title)}</a>\n"
                "Теперь обычные участники вашей группы должны быть подписаны на эту площадку, чтобы писать сообщения.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await callback.answer("ОП запущена")

    @router.callback_query(F.data.regexp(r"^ads:mandatory:reject:\d+$"))
    async def reject(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        buyer_id = None
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(
                    select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update()
                )).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                deal.status = "rejected"
                deal.rejected_at = datetime.now(timezone.utc)
                placement = (await session.execute(
                    select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "mandatory",
                    ).with_for_update()
                )).scalar_one_or_none()
                if placement is not None:
                    placement.status = "rejected"
                buyer_id = deal.buyer_user_id
        if buyer_id is not None:
            try:
                await bot.send_message(buyer_id, "❌ Рекламодатель отклонил вашу заявку на ОП.")
            except Exception:
                pass
        if callback.message is not None:
            await callback.message.edit_text("❌ Заявка на ОП отклонена.")
        await callback.answer("Заявка отклонена")

    return router
