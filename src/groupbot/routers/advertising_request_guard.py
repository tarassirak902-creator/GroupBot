from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.routers import advertising_requests as advertising_requests_module
from groupbot.routers.advertising_settlement import settlement_keyboard
from groupbot.services.subscriptions import active_subscription_for_group


_base_deal_keyboard = advertising_requests_module._deal_keyboard


def _deal_keyboard_with_settlement(deal: AdvertisingDeal, viewer_id: int):
    if deal.status == "finished_waiting_confirmation":
        return settlement_keyboard(deal.id)
    return _base_deal_keyboard(deal, viewer_id)


# This module is loaded after advertising_sales_nav has installed its seller controls.
# Extend that shared keyboard rather than replacing the sales navigation behavior.
advertising_requests_module._deal_keyboard = _deal_keyboard_with_settlement


async def _available_listing(
    session: AsyncSession,
    *,
    listing_id: int,
    buyer_user_id: int,
    lock: bool = False,
) -> AdvertisingListing | None:
    query = (
        select(AdvertisingListing, GroupOwner.user_id)
        .join(Group, Group.chat_id == AdvertisingListing.chat_id)
        .join(
            GroupOwner,
            (GroupOwner.chat_id == Group.chat_id) & GroupOwner.is_current.is_(True),
        )
        .where(
            AdvertisingListing.id == listing_id,
            AdvertisingListing.is_active.is_(True),
            AdvertisingListing.owner_user_id != buyer_user_id,
            Group.status == GroupStatus.active.value,
        )
        .limit(1)
    )
    if lock:
        query = query.with_for_update(of=AdvertisingListing)
    row = (await session.execute(query)).first()
    if row is None:
        return None
    listing, current_owner_id = row
    if int(current_owner_id) != listing.owner_user_id:
        return None
    if await active_subscription_for_group(session, listing.chat_id) is None:
        return None
    return listing


async def _owned_active_group(
    session: AsyncSession,
    *,
    chat_id: int,
    owner_user_id: int,
) -> bool:
    row = (
        await session.execute(
            select(Group.chat_id)
            .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
            .where(
                Group.chat_id == chat_id,
                Group.status == GroupStatus.active.value,
                GroupOwner.user_id == owner_user_id,
                GroupOwner.is_current.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    return await active_subscription_for_group(session, chat_id) is not None


def _unavailable_text() -> str:
    return (
        "Эта рекламная площадка сейчас недоступна: объявление выключено, "
        "группа отключена, владелец сменился или подписка закончилась."
    )


def create_advertising_request_guard_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_request_guard")

    @router.callback_query(F.data.startswith("ads:request:"))
    async def choose_request_type(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        try:
            listing_id = int((callback.data or "").rsplit(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        async with session_factory() as session:
            listing = await _available_listing(
                session,
                listing_id=listing_id,
                buyer_user_id=callback.from_user.id,
            )
        if listing is None:
            await callback.answer(_unavailable_text(), show_alert=True)
            return
        await callback.message.edit_text(
            "📨 <b>Отправить запрос</b>\n\nНа какой вид рекламы отправить заявку?",
            parse_mode="HTML",
            reply_markup=advertising_requests_module._request_type_keyboard(listing),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:req:type:\d+:(post|mandatory|both)$"))
    async def send_request(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            listing_id = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer("Некорректное объявление.", show_alert=True)
            return
        kind = parts[4]

        async with session_factory() as session:
            async with session.begin():
                listing = await _available_listing(
                    session,
                    listing_id=listing_id,
                    buyer_user_id=callback.from_user.id,
                    lock=True,
                )
                if listing is None:
                    await callback.answer(_unavailable_text(), show_alert=True)
                    return

                requested_post = kind in {"post", "both"}
                requested_mandatory = kind in {"mandatory", "both"}
                if requested_post and not listing.offers_post:
                    await callback.answer("Посты в этом объявлении больше не продаются.", show_alert=True)
                    return
                if requested_mandatory and not listing.offers_mandatory:
                    await callback.answer("ОП в этом объявлении больше не продаётся.", show_alert=True)
                    return

                existing = (
                    await session.execute(
                        select(AdvertisingDeal).where(
                            AdvertisingDeal.listing_id == listing.id,
                            AdvertisingDeal.buyer_user_id == callback.from_user.id,
                            AdvertisingDeal.status == "pending",
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    await callback.answer(
                        "У вас уже есть заявка на эту площадку, ожидающая решения.",
                        show_alert=True,
                    )
                    return

                deal = AdvertisingDeal(
                    listing_id=listing.id,
                    seller_user_id=listing.owner_user_id,
                    buyer_user_id=callback.from_user.id,
                    requested_post=requested_post,
                    requested_mandatory=requested_mandatory,
                    status="pending",
                    agreed_terms_json={
                        "post_price_stars": listing.post_price_stars if requested_post else None,
                        "post_interval_minutes": listing.post_interval_minutes if requested_post else None,
                        "post_terms": listing.post_terms_json if requested_post else None,
                        "mandatory_price_stars": listing.mandatory_price_stars if requested_mandatory else None,
                        "mandatory_terms": listing.mandatory_terms_json if requested_mandatory else None,
                    },
                )
                session.add(deal)
                await session.flush()
                deal_id = deal.id
                seller_id = deal.seller_user_id
                title = listing.group_title_snapshot
                kind_label = advertising_requests_module._kind_text(deal)

        try:
            await bot.send_message(
                seller_id,
                "📥 <b>Новая рекламная заявка</b>\n\n"
                f"🏠 Площадка: <b>{title}</b>\n"
                f"👤 Покупатель: <b>{callback.from_user.full_name}</b>\n"
                f"📌 Запрос: <b>{kind_label}</b>\n\n"
                "Вы можете связаться с покупателем и обсудить условия перед принятием заявки.",
                parse_mode="HTML",
                reply_markup=advertising_requests_module._seller_request_keyboard(deal_id, callback.from_user.id),
            )
        except Exception:
            pass

        async with session_factory() as session:
            loaded = (
                await session.execute(
                    select(AdvertisingDeal, AdvertisingListing)
                    .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                    .where(AdvertisingDeal.id == deal_id)
                )
            ).first()
        if loaded is None:
            return
        deal, listing = loaded
        await callback.message.edit_text(
            "✅ <b>Заявка отправлена</b>\n\n" + advertising_requests_module._deal_text(deal, listing),
            parse_mode="HTML",
            reply_markup=advertising_requests_module._deal_keyboard(deal, callback.from_user.id),
        )
        await callback.answer("Заявка отправлена")

    @router.callback_query(F.data.regexp(r"^ads:mutual:accept:\d+$"))
    async def accept_mutual(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            async with session.begin():
                deal = (
                    await session.execute(
                        select(AdvertisingDeal)
                        .where(AdvertisingDeal.id == deal_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                terms = dict(deal.agreed_terms_json or {})
                if not terms.get("mutual_op"):
                    await callback.answer("Это не заявка на взаимное ОП.", show_alert=True)
                    return
                try:
                    group_a = int(terms["group_a_chat_id"])
                    group_b = int(terms["group_b_chat_id"])
                except (KeyError, TypeError, ValueError):
                    await callback.answer("Данные взаимного ОП повреждены.", show_alert=True)
                    return

                listing = (
                    await session.execute(
                        select(AdvertisingListing)
                        .where(
                            AdvertisingListing.id == deal.listing_id,
                            AdvertisingListing.owner_user_id == deal.seller_user_id,
                            AdvertisingListing.is_active.is_(True),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                buyer_ready = await _owned_active_group(
                    session,
                    chat_id=group_a,
                    owner_user_id=deal.buyer_user_id,
                )
                seller_ready = await _owned_active_group(
                    session,
                    chat_id=group_b,
                    owner_user_id=deal.seller_user_id,
                )
                if listing is None or listing.chat_id != group_b or not buyer_ready or not seller_ready:
                    await callback.answer(
                        "Взаимное ОП нельзя запустить: одна из групп отключена, сменила владельца или у её владельца закончилась подписка.",
                        show_alert=True,
                    )
                    return

                title_a = str(terms.get("group_a_title") or "Группа A")
                title_b = str(terms.get("group_b_title") or "Группа B")
                mode = str(terms.get("mode") or "days")
                qty = max(int(terms.get("quantity") or 1), 1)
                try:
                    link_b = await bot.create_chat_invite_link(
                        group_b,
                        name=f"Mimorus mutual #{deal.id} A-B",
                    )
                    link_a = await bot.create_chat_invite_link(
                        group_a,
                        name=f"Mimorus mutual #{deal.id} B-A",
                    )
                except Exception:
                    await callback.answer(
                        "Не удалось создать рекламные ссылки. Проверьте право Mimorus приглашать пользователей в обеих группах.",
                        show_alert=True,
                    )
                    return

                ends_at = now + timedelta(days=qty) if mode == "days" else None
                session.add_all([
                    AdvertisingMutualOpDirection(
                        deal_id=deal.id,
                        source_chat_id=group_a,
                        target_chat_id=group_b,
                        source_title=title_a,
                        target_title=title_b,
                        status="active",
                        mode=mode,
                        quantity=qty,
                        invite_link=link_b.invite_link,
                        starts_at=now,
                        ends_at=ends_at,
                    ),
                    AdvertisingMutualOpDirection(
                        deal_id=deal.id,
                        source_chat_id=group_b,
                        target_chat_id=group_a,
                        source_title=title_b,
                        target_title=title_a,
                        status="active",
                        mode=mode,
                        quantity=qty,
                        invite_link=link_a.invite_link,
                        starts_at=now,
                        ends_at=ends_at,
                    ),
                ])
                deal.status = "accepted"
                deal.accepted_at = now
                deal.started_at = now
                buyer_id = deal.buyer_user_id

        condition = f"{qty} дн." if mode == "days" else f"{qty} действующих участников каждой стороне"
        for user_id in (buyer_id, callback.from_user.id):
            try:
                await bot.send_message(
                    user_id,
                    "🤝 <b>Взаимное ОП запущено</b>\n\n"
                    f"🏠 {escape(title_a)} ↔ {escape(title_b)}\n"
                    f"🎯 Условие: <b>{condition}</b>\n\n"
                    "Каждое направление завершится отдельно, когда выполнит своё условие.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        if callback.message is not None:
            await callback.message.edit_text("✅ Взаимное ОП принято и запущено.")
        await callback.answer("Взаимное ОП запущено")

    return router
