from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.routers import advertising_requests as advertising_requests_module
from groupbot.routers.advertising_settlement import settlement_keyboard
from groupbot.services.subscriptions import active_subscription_for_group


POST_DAILY_LIMIT = 1
MANDATORY_DAILY_LIMIT = 3

_base_deal_keyboard = advertising_requests_module._deal_keyboard


def _deal_keyboard_with_settlement(deal: AdvertisingDeal, viewer_id: int):
    if deal.status == "finished_waiting_confirmation":
        return settlement_keyboard(deal.id)
    return _base_deal_keyboard(deal, viewer_id)


# Loaded after advertising_sales_nav has installed seller controls. Extend the
# shared deal keyboard so every "open deal" path exposes settlement actions.
advertising_requests_module._deal_keyboard = _deal_keyboard_with_settlement


async def _available_listing(
    session: AsyncSession,
    *,
    listing_id: int,
    buyer_user_id: int,
) -> AdvertisingListing | None:
    row = (
        await session.execute(
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
    ).first()
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


async def _seller_listing_ready(
    session: AsyncSession,
    *,
    listing: AdvertisingListing,
    seller_user_id: int,
) -> bool:
    if not listing.is_active or listing.owner_user_id != seller_user_id:
        return False
    return await _owned_active_group(
        session,
        chat_id=listing.chat_id,
        owner_user_id=seller_user_id,
    )


async def _mandatory_target_ready(
    bot: Bot,
    session: AsyncSession,
    *,
    target_chat_id: int,
    buyer_user_id: int,
) -> bool:
    group = (
        await session.execute(select(Group).where(Group.chat_id == target_chat_id))
    ).scalar_one_or_none()
    if group is not None:
        return await _owned_active_group(
            session,
            chat_id=target_chat_id,
            owner_user_id=buyer_user_id,
        )
    try:
        await bot.get_chat_member_count(target_chat_id)
    except Exception:
        return False
    return True


def _duration_days(deal: AdvertisingDeal) -> int:
    terms = (deal.agreed_terms_json or {}).get("post_terms") or {}
    try:
        return max(int(terms.get("duration_days") or 1), 1)
    except (TypeError, ValueError):
        return 1


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

    @router.callback_query(F.data.regexp(r"^ads:mandatory:accept:\d+$"))
    async def accept_mandatory(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        buyer_id: int | None = None
        target_title = ""
        target_url = ""
        seller_group = ""
        post_started = False
        duration_days = 1
        mode = "days"
        quantity = 1
        total_price = 0

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

                listing = (
                    await session.execute(
                        select(AdvertisingListing)
                        .where(AdvertisingListing.id == deal.listing_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if listing is None or not await _seller_listing_ready(
                    session,
                    listing=listing,
                    seller_user_id=callback.from_user.id,
                ):
                    await callback.answer(
                        "Заявку нельзя запустить: рекламная группа отключена, сменила владельца или подписка закончилась.",
                        show_alert=True,
                    )
                    return

                used_op = int((await session.execute(
                    select(func.count()).select_from(AdvertisingDeal).where(
                        AdvertisingDeal.listing_id == deal.listing_id,
                        AdvertisingDeal.accepted_at >= day_start,
                        AdvertisingDeal.requested_mandatory.is_(True),
                    )
                )).scalar_one())
                if used_op >= MANDATORY_DAILY_LIMIT:
                    await callback.answer("Лимит ОП на сегодня уже использован: 3 из 3.", show_alert=True)
                    return
                if deal.requested_post:
                    used_post = int((await session.execute(
                        select(func.count()).select_from(AdvertisingDeal).where(
                            AdvertisingDeal.listing_id == deal.listing_id,
                            AdvertisingDeal.accepted_at >= day_start,
                            AdvertisingDeal.requested_post.is_(True),
                        )
                    )).scalar_one())
                    if used_post >= POST_DAILY_LIMIT:
                        await callback.answer("Лимит рекламных постов на сегодня уже использован: 1 из 1.", show_alert=True)
                        return

                mandatory = (
                    await session.execute(
                        select(AdvertisingPlacement).where(
                            AdvertisingPlacement.deal_id == deal.id,
                            AdvertisingPlacement.kind == "mandatory",
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if mandatory is None or mandatory.status != "ready":
                    await callback.answer("Данные ОП не готовы.", show_alert=True)
                    return
                cfg = dict(mandatory.config_json or {})
                target_chat_id = cfg.get("target_chat_id")
                if not isinstance(target_chat_id, int):
                    await callback.answer("Группа ОП не определена.", show_alert=True)
                    return
                if not await _mandatory_target_ready(
                    bot,
                    session,
                    target_chat_id=target_chat_id,
                    buyer_user_id=deal.buyer_user_id,
                ):
                    await callback.answer(
                        "ОП нельзя запустить: целевая группа отключена, сменила владельца, потеряла подписку или Mimorus больше не имеет к ней доступа.",
                        show_alert=True,
                    )
                    return

                mode = str(cfg.get("mode") or "days")
                quantity = max(int(cfg.get("quantity") or 1), 1)
                total_price = int(cfg.get("total_price_stars") or 0)
                target_title = str(cfg.get("target_title") or "Группа")
                target_url = str(cfg.get("target_url") or "")
                if mode == "days":
                    mandatory.ends_at = now + timedelta(days=quantity)
                else:
                    try:
                        baseline = await bot.get_chat_member_count(target_chat_id)
                    except Exception:
                        await callback.answer(
                            "Не удалось получить текущее число участников целевой группы. ОП не запущена.",
                            show_alert=True,
                        )
                        return
                    cfg["baseline_member_count"] = baseline
                    cfg["target_member_count"] = baseline + quantity
                    mandatory.ends_at = None
                cfg["started_at"] = now.isoformat()
                mandatory.config_json = cfg
                mandatory.status = "active"
                mandatory.starts_at = now

                if deal.requested_post:
                    post = (
                        await session.execute(
                            select(AdvertisingPlacement).where(
                                AdvertisingPlacement.deal_id == deal.id,
                                AdvertisingPlacement.kind == "post",
                            ).with_for_update()
                        )
                    ).scalar_one_or_none()
                    if post is None or post.status != "ready":
                        await callback.answer("Рекламный пост не готов.", show_alert=True)
                        return
                    duration_days = _duration_days(deal)
                    post.status = "active"
                    post.starts_at = now
                    post.ends_at = now + timedelta(days=duration_days)
                    post_cfg = dict(post.config_json or {})
                    post_cfg["duration_days"] = duration_days
                    post.config_json = post_cfg
                    post_started = True

                deal.status = "accepted"
                deal.accepted_at = now
                deal.started_at = now
                buyer_id = deal.buyer_user_id
                seller_group = listing.group_title_snapshot

        volume_text = f"{quantity} дн." if mode == "days" else f"{quantity} подписчиков"
        if buyer_id is not None:
            try:
                extra = f"\n📣 Рекламный пост также запущен на <b>{duration_days} дн.</b>" if post_started else ""
                await bot.send_message(
                    buyer_id,
                    "✅ <b>Рекламодатель одобрил вашу заявку</b>\n\n"
                    f"🏠 Площадка: <b>{escape(seller_group)}</b>\n"
                    f"🎯 ОП на: <a href=\"{target_url}\">{escape(target_title)}</a>\n"
                    f"📐 Объём ОП: <b>{volume_text}</b>\n"
                    f"⭐ Стоимость ОП: <b>{total_price} ⭐</b>\n\n"
                    f"🚀 Обязательная подписка включена автоматически.{extra}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        if callback.message is not None:
            extra = f"\n📣 Пост: <b>запущен на {duration_days} дн.</b>" if post_started else ""
            await callback.message.edit_text(
                "✅ <b>Заявка одобрена и запущена</b>\n\n"
                f"🎯 ОП: <a href=\"{target_url}\">{escape(target_title)}</a>\n"
                f"📐 Объём: <b>{volume_text}</b>{extra}\n\n"
                "Обычные участники вашей группы должны быть подписаны на указанную площадку, чтобы писать сообщения.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await callback.answer("Реклама запущена")

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
