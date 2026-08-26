from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.models import Group, GroupOwner, GroupStatus


class MutualOpState(StatesGroup):
    waiting_quantity = State()


def request_type_keyboard_with_mutual(listing: AdvertisingListing) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if listing.offers_post:
        rows.append([InlineKeyboardButton(text="📣 Рекламный пост", callback_data=f"ads:req:type:{listing.id}:post")])
    if listing.offers_mandatory:
        rows.append([InlineKeyboardButton(text="✅ Купить ОП", callback_data=f"ads:req:type:{listing.id}:mandatory")])
        rows.append([InlineKeyboardButton(text="🤝 Взаимное ОП", callback_data=f"ads:req:type:{listing.id}:mutual")])
    if listing.offers_post and listing.offers_mandatory:
        rows.append([InlineKeyboardButton(text="📣 + ✅ Пост и ОП", callback_data=f"ads:req:type:{listing.id}:both")])
    rows.append([InlineKeyboardButton(text="◀️ К объявлению", callback_data=f"ads:listing:{listing.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _group_keyboard(listing_id: int, groups: list[tuple[int, str | None]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🏠 {(title or 'Группа')[:52]}", callback_data=f"ads:mutual:group:{listing_id}:{chat_id}")]
        for chat_id, title in groups
    ]
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="ads:buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mode_keyboard(listing_id: int, source_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 По дням", callback_data=f"ads:mutual:mode:{listing_id}:{source_chat_id}:days")],
        [InlineKeyboardButton(text="👥 По участникам", callback_data=f"ads:mutual:mode:{listing_id}:{source_chat_id}:subscribers")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="ads:buy")],
    ])


def _seller_keyboard(deal_id: int, buyer_id: int, source_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Открыть предлагаемую группу", callback_data=f"ads:mutual:open_group:{source_chat_id}")],
        [InlineKeyboardButton(text="💬 Связаться", url=f"tg://user?id={buyer_id}")],
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"ads:mutual:accept:{deal_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:mutual:reject:{deal_id}"),
        ],
    ])


def create_advertising_mutual_op_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_mutual_op")

    async def _owned_groups(user_id: int) -> list[tuple[int, str | None]]:
        async with session_factory() as session:
            rows = (await session.execute(
                select(Group.chat_id, Group.title)
                .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                .where(
                    GroupOwner.user_id == user_id,
                    GroupOwner.is_current.is_(True),
                    Group.status == GroupStatus.active.value,
                )
                .order_by(Group.title, Group.chat_id)
            )).all()
        return list(rows)

    @router.callback_query(F.data.regexp(r"^ads:req:type:\d+:mutual$"))
    async def start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        listing_id = int((callback.data or "").split(":")[3])
        async with session_factory() as session:
            listing = (await session.execute(select(AdvertisingListing).where(
                AdvertisingListing.id == listing_id,
                AdvertisingListing.is_active.is_(True),
                AdvertisingListing.offers_mandatory.is_(True),
            ))).scalar_one_or_none()
        if listing is None or listing.owner_user_id == callback.from_user.id:
            await callback.answer("Объявление недоступно.", show_alert=True)
            return
        groups = await _owned_groups(callback.from_user.id)
        if not groups:
            await callback.answer("Для взаимного ОП нужна ваша подключённая группа.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text(
            "🤝 <b>Взаимное ОП</b>\n\n"
            "Выберите свою группу, которую хотите предложить для взаимной обязательной подписки.",
            parse_mode="HTML",
            reply_markup=_group_keyboard(listing_id, groups),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:mutual:group:\d+:-?\d+$"))
    async def choose_group(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        listing_id = int(parts[3])
        source_chat_id = int(parts[4])
        async with session_factory() as session:
            row = (await session.execute(
                select(Group, GroupOwner)
                .join(GroupOwner, GroupOwner.chat_id == Group.chat_id)
                .where(
                    Group.chat_id == source_chat_id,
                    GroupOwner.user_id == callback.from_user.id,
                    GroupOwner.is_current.is_(True),
                    Group.status == GroupStatus.active.value,
                )
            )).first()
            listing = (await session.execute(select(AdvertisingListing).where(
                AdvertisingListing.id == listing_id,
                AdvertisingListing.is_active.is_(True),
            ))).scalar_one_or_none()
        if row is None or listing is None:
            await callback.answer("Группа или объявление недоступны.", show_alert=True)
            return
        try:
            me = await bot.get_me()
            for chat_id in (source_chat_id, listing.chat_id):
                member = await bot.get_chat_member(chat_id, me.id)
                if member.status not in {"administrator", "creator"}:
                    raise RuntimeError
        except Exception:
            await callback.answer("Mimorus должен быть администратором в обеих группах.", show_alert=True)
            return
        group, _ = row
        await callback.message.edit_text(
            "🤝 <b>Взаимное ОП</b>\n\n"
            f"🏠 Ваша группа: <b>{escape(group.title or 'Группа')}</b>\n"
            f"🔄 Вторая группа: <b>{escape(listing.group_title_snapshot)}</b>\n\n"
            "Как считать выполнение взаимного ОП?",
            parse_mode="HTML",
            reply_markup=_mode_keyboard(listing_id, source_chat_id),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:mutual:mode:\d+:-?\d+:(days|subscribers)$"))
    async def choose_mode(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        listing_id = int(parts[3])
        source_chat_id = int(parts[4])
        mode = parts[5]
        await state.set_state(MutualOpState.waiting_quantity)
        await state.update_data(mutual_listing_id=listing_id, mutual_source_chat_id=source_chat_id, mutual_mode=mode)
        prompt = (
            "Введите количество дней взаимного ОП, например: <code>7</code>."
            if mode == "days"
            else "Введите количество действующих участников, которое каждая группа должна привести другой, например: <code>100</code>."
        )
        await callback.message.edit_text(
            "🤝 <b>Условие взаимного ОП</b>\n\n" + prompt,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="ads:buy")]]),
        )
        await callback.answer()

    @router.message(MutualOpState.waiting_quantity, F.chat.type == "private")
    async def quantity(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        try:
            value = int((message.text or "").strip())
        except ValueError:
            value = 0
        try:
            await message.delete()
        except Exception:
            pass
        data = await state.get_data()
        listing_id = data.get("mutual_listing_id")
        source_chat_id = data.get("mutual_source_chat_id")
        mode = str(data.get("mutual_mode") or "")
        max_value = 365 if mode == "days" else 1_000_000
        if value <= 0 or value > max_value or not isinstance(listing_id, int) or not isinstance(source_chat_id, int):
            await message.answer(f"Введите целое число от 1 до {max_value}.")
            return
        async with session_factory() as session:
            async with session.begin():
                listing = (await session.execute(select(AdvertisingListing).where(
                    AdvertisingListing.id == listing_id,
                    AdvertisingListing.is_active.is_(True),
                    AdvertisingListing.offers_mandatory.is_(True),
                ).with_for_update())).scalar_one_or_none()
                source = (await session.execute(select(Group).join(GroupOwner, GroupOwner.chat_id == Group.chat_id).where(
                    Group.chat_id == source_chat_id,
                    GroupOwner.user_id == message.from_user.id,
                    GroupOwner.is_current.is_(True),
                    Group.status == GroupStatus.active.value,
                ))).scalar_one_or_none()
                if listing is None or source is None or listing.owner_user_id == message.from_user.id:
                    await state.clear()
                    await message.answer("Группа или объявление больше недоступны.")
                    return
                existing = (await session.execute(select(AdvertisingDeal.id).where(
                    AdvertisingDeal.listing_id == listing.id,
                    AdvertisingDeal.buyer_user_id == message.from_user.id,
                    AdvertisingDeal.status == "pending",
                ).limit(1))).scalar_one_or_none()
                if existing is not None:
                    await state.clear()
                    await message.answer("У вас уже есть заявка на эту площадку, ожидающая решения.")
                    return
                deal = AdvertisingDeal(
                    listing_id=listing.id,
                    seller_user_id=listing.owner_user_id,
                    buyer_user_id=message.from_user.id,
                    requested_post=False,
                    requested_mandatory=True,
                    status="pending",
                    agreed_terms_json={
                        "mutual_op": True,
                        "mode": mode,
                        "quantity": value,
                        "group_a_chat_id": source.chat_id,
                        "group_a_title": source.title or "Группа A",
                        "group_b_chat_id": listing.chat_id,
                        "group_b_title": listing.group_title_snapshot,
                    },
                )
                session.add(deal)
                await session.flush()
                deal_id = deal.id
                seller_id = deal.seller_user_id
                source_title = source.title or "Группа A"
                target_title = listing.group_title_snapshot
                source_members = await bot.get_chat_member_count(source.chat_id)
                target_members = await bot.get_chat_member_count(listing.chat_id)
        await state.clear()
        condition = f"{value} дн." if mode == "days" else f"+{value} действующих участников каждой стороне"
        try:
            await bot.send_message(
                seller_id,
                "🤝 <b>Запрос на взаимное ОП</b>\n\n"
                f"🏠 Ваша группа: <b>{escape(target_title)}</b>\n"
                f"👥 Участников: <b>{target_members:,}</b>\n\n".replace(",", " ") +
                f"🔄 Предлагаемая группа: <b>{escape(source_title)}</b>\n"
                f"👥 Участников: <b>{source_members:,}</b>\n\n".replace(",", " ") +
                f"🎯 Условие: <b>{condition}</b>\n\n"
                "После принятия обязательная подписка включится одновременно в обеих группах.",
                parse_mode="HTML",
                reply_markup=_seller_keyboard(deal_id, message.from_user.id, source_chat_id),
            )
        except Exception:
            pass
        await message.answer(
            "✅ <b>Запрос на взаимное ОП отправлен</b>\n\n"
            f"🏠 {escape(source_title)} ↔ {escape(target_title)}\n"
            f"🎯 {condition}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")]]),
        )

    @router.callback_query(F.data.regexp(r"^ads:mutual:open_group:-?\d+$"))
    async def open_group(callback: CallbackQuery, bot: Bot) -> None:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
        try:
            chat = await bot.get_chat(chat_id)
            url = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
            if not url:
                await callback.answer("У группы нет доступной ссылки.", show_alert=True)
                return
            if callback.message is not None:
                await callback.message.answer(f"🏠 {escape(chat.title or 'Группа')}\n{url}")
        except Exception:
            await callback.answer("Не удалось открыть группу.", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:mutual:accept:\d+$"))
    async def accept(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending":
                    await callback.answer("Заявка недоступна или уже обработана.", show_alert=True)
                    return
                terms = dict(deal.agreed_terms_json or {})
                if not terms.get("mutual_op"):
                    await callback.answer("Это не заявка на взаимное ОП.", show_alert=True)
                    return
                group_a = int(terms["group_a_chat_id"])
                group_b = int(terms["group_b_chat_id"])
                title_a = str(terms.get("group_a_title") or "Группа A")
                title_b = str(terms.get("group_b_title") or "Группа B")
                mode = str(terms.get("mode") or "days")
                qty = max(int(terms.get("quantity") or 1), 1)
                try:
                    link_b = await bot.create_chat_invite_link(group_b, name=f"Mimorus mutual #{deal.id} A-B")
                    link_a = await bot.create_chat_invite_link(group_a, name=f"Mimorus mutual #{deal.id} B-A")
                except Exception:
                    await callback.answer("Не удалось создать рекламные ссылки. Проверьте право бота приглашать пользователей в обеих группах.", show_alert=True)
                    return
                ends_at = now + timedelta(days=qty) if mode == "days" else None
                session.add_all([
                    AdvertisingMutualOpDirection(
                        deal_id=deal.id, source_chat_id=group_a, target_chat_id=group_b,
                        source_title=title_a, target_title=title_b, status="active", mode=mode,
                        quantity=qty, invite_link=link_b.invite_link, starts_at=now, ends_at=ends_at,
                    ),
                    AdvertisingMutualOpDirection(
                        deal_id=deal.id, source_chat_id=group_b, target_chat_id=group_a,
                        source_title=title_b, target_title=title_a, status="active", mode=mode,
                        quantity=qty, invite_link=link_a.invite_link, starts_at=now, ends_at=ends_at,
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

    @router.callback_query(F.data.regexp(r"^ads:mutual:reject:\d+$"))
    async def reject(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.seller_user_id != callback.from_user.id or deal.status != "pending" or not (deal.agreed_terms_json or {}).get("mutual_op"):
                    await callback.answer("Заявка недоступна.", show_alert=True)
                    return
                deal.status = "rejected"
                deal.rejected_at = datetime.now(timezone.utc)
                buyer_id = deal.buyer_user_id
        try:
            await bot.send_message(buyer_id, "❌ Запрос на взаимное ОП отклонён.")
        except Exception:
            pass
        if callback.message is not None:
            await callback.message.edit_text("❌ Запрос на взаимное ОП отклонён.")
        await callback.answer("Отклонено")

    return router
