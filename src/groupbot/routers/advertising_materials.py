from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement


class AdvertisingMaterialState(StatesGroup):
    waiting_post = State()
    waiting_mandatory = State()


def _materials_keyboard(deal: AdvertisingDeal, placements: list[AdvertisingPlacement]) -> InlineKeyboardMarkup:
    by_kind = {row.kind: row for row in placements}
    rows: list[list[InlineKeyboardButton]] = []
    if deal.requested_post:
        ready = by_kind.get("post") is not None and by_kind["post"].status == "ready"
        rows.append([InlineKeyboardButton(
            text=("✅" if ready else "📣") + " Рекламный пост",
            callback_data=f"ads:materials:post:{deal.id}",
        )])
    if deal.requested_mandatory:
        ready = by_kind.get("mandatory") is not None and by_kind["mandatory"].status == "ready"
        rows.append([InlineKeyboardButton(
            text=("✅" if ready else "🔔") + " Канал/группа для ОП",
            callback_data=f"ads:materials:mandatory:{deal.id}",
        )])
    rows.append([InlineKeyboardButton(text="📨 Открыть сделку", callback_data=f"ads:deal:{deal.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _materials_text(deal: AdvertisingDeal, listing: AdvertisingListing, placements: list[AdvertisingPlacement]) -> str:
    by_kind = {row.kind: row for row in placements}
    lines = [
        "📦 <b>Материалы рекламной сделки</b>",
        "",
        f"🏠 Площадка: <b>{listing.group_title_snapshot}</b>",
        "",
    ]
    if deal.requested_post:
        ready = by_kind.get("post") is not None and by_kind["post"].status == "ready"
        lines.append(f"{'✅' if ready else '⏳'} Рекламный пост: <b>{'готов' if ready else 'не передан'}</b>")
    if deal.requested_mandatory:
        placement = by_kind.get("mandatory")
        ready = placement is not None and placement.status == "ready"
        title = ""
        if ready:
            title = str((placement.config_json or {}).get("sponsor_title") or "спонсор")
        lines.append(f"{'✅' if ready else '⏳'} ОП: <b>{title if ready else 'спонсор не указан'}</b>")
    all_ready = bool(placements) and all(row.status == "ready" for row in placements)
    lines.extend([
        "",
        "✅ Все материалы готовы. Рекламодатель может запускать размещение." if all_ready else "Выберите материал, который хотите передать или заменить.",
    ])
    return "\n".join(lines)


def _normalise_sponsor(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if text.startswith(prefix):
            tail = text[len(prefix):].split("?", 1)[0].strip("/")
            if tail and not tail.startswith("+"):
                return "@" + tail.lstrip("@")
            return None
    if text.startswith("@") and len(text) > 1:
        return text
    return None


def create_advertising_materials_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="advertising_materials")

    async def _load(deal_id: int) -> tuple[AdvertisingDeal, AdvertisingListing, list[AdvertisingPlacement]] | None:
        async with session_factory() as session:
            deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id))).scalar_one_or_none()
            if deal is None:
                return None
            listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id))).scalar_one_or_none()
            if listing is None:
                return None
            placements = list((await session.execute(
                select(AdvertisingPlacement).where(AdvertisingPlacement.deal_id == deal_id).order_by(AdvertisingPlacement.id)
            )).scalars().all())
            return deal, listing, placements

    async def _notify_if_ready(bot: Bot, deal_id: int) -> bool:
        loaded = await _load(deal_id)
        if loaded is None:
            return False
        deal, listing, placements = loaded
        if not placements or not all(row.status == "ready" for row in placements):
            return False
        try:
            await bot.send_message(
                deal.seller_user_id,
                "✅ <b>Материалы рекламной сделки готовы</b>\n\n"
                f"🏠 Площадка: <b>{listing.group_title_snapshot}</b>\n\n"
                "Покупатель передал все необходимые материалы. Сделку можно открыть и перейти к запуску рекламы.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📨 Открыть сделку", callback_data=f"ads:deal:{deal.id}")],
                    [InlineKeyboardButton(text="💬 Связаться с покупателем", url=f"tg://user?id={deal.buyer_user_id}")],
                ]),
            )
        except Exception:
            pass
        return True

    @router.callback_query(F.data.startswith("ads:materials:"), ~F.data.startswith("ads:materials:post:"), ~F.data.startswith("ads:materials:mandatory:"))
    async def materials_home(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректная сделка.", show_alert=True)
            return
        loaded = await _load(deal_id)
        if loaded is None:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return
        deal, listing, placements = loaded
        if callback.from_user.id != deal.buyer_user_id:
            await callback.answer("Материалы передаёт покупатель.", show_alert=True)
            return
        if deal.status != "accepted":
            await callback.answer("Материалы можно передавать только после принятия заявки.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text(
            _materials_text(deal, listing, placements),
            parse_mode="HTML",
            reply_markup=_materials_keyboard(deal, placements),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ads:materials:post:"))
    async def choose_post(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        loaded = await _load(deal_id)
        if loaded is None or callback.from_user.id != loaded[0].buyer_user_id or loaded[0].status != "accepted" or not loaded[0].requested_post:
            await callback.answer("Этот материал недоступен.", show_alert=True)
            return
        await state.set_state(AdvertisingMaterialState.waiting_post)
        await state.update_data(advertising_material_deal_id=deal_id)
        await callback.message.edit_text(
            "📣 <b>Рекламный пост</b>\n\n"
            "Отправьте или перешлите сюда готовый рекламный пост. Можно использовать текст, фото, видео или другой обычный Telegram-пост.\n\n"
            "Mimorus сохранит именно это сообщение для последующей публикации.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ads:materials:{deal_id}")]
            ]),
        )
        await callback.answer()

    @router.message(AdvertisingMaterialState.waiting_post, F.chat.type == "private")
    async def receive_post(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        deal_id = data.get("advertising_material_deal_id")
        if not isinstance(deal_id, int):
            await state.clear()
            return
        loaded = await _load(deal_id)
        if loaded is None or message.from_user.id != loaded[0].buyer_user_id or loaded[0].status != "accepted":
            await state.clear()
            await message.answer("Эта сделка больше не принимает материалы.")
            return
        if message.text and message.text.startswith("/"):
            await message.answer("Отправьте сам рекламный пост, а не команду.")
            return
        async with session_factory() as session:
            async with session.begin():
                placement = (await session.execute(select(AdvertisingPlacement).where(
                    AdvertisingPlacement.deal_id == deal_id,
                    AdvertisingPlacement.kind == "post",
                ).with_for_update())).scalar_one_or_none()
                if placement is None:
                    await state.clear()
                    await message.answer("Размещение поста не найдено.")
                    return
                cfg = dict(placement.config_json or {})
                cfg.update({
                    "source_chat_id": message.chat.id,
                    "source_message_id": message.message_id,
                    "content_type": message.content_type,
                })
                placement.config_json = cfg
                placement.status = "ready"
        await state.clear()
        await message.answer(
            "✅ Рекламный пост сохранён.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Материалы сделки", callback_data=f"ads:materials:{deal_id}")]
            ]),
        )
        await _notify_if_ready(bot, deal_id)

    @router.callback_query(F.data.startswith("ads:materials:mandatory:"))
    async def choose_mandatory(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        try:
            deal_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        loaded = await _load(deal_id)
        if loaded is None or callback.from_user.id != loaded[0].buyer_user_id or loaded[0].status != "accepted" or not loaded[0].requested_mandatory:
            await callback.answer("Этот материал недоступен.", show_alert=True)
            return
        await state.set_state(AdvertisingMaterialState.waiting_mandatory)
        await state.update_data(advertising_material_deal_id=deal_id)
        await callback.message.edit_text(
            "🔔 <b>Канал или группа для ОП</b>\n\n"
            "Отправьте публичный @username или ссылку вида <code>https://t.me/example</code>.\n\n"
            "Mimorus должен быть добавлен туда администратором, иначе бот не сможет надёжно проверять подписку пользователей.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ads:materials:{deal_id}")]
            ]),
        )
        await callback.answer()

    @router.message(AdvertisingMaterialState.waiting_mandatory, F.chat.type == "private")
    async def receive_mandatory(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        deal_id = data.get("advertising_material_deal_id")
        if not isinstance(deal_id, int):
            await state.clear()
            return
        target = _normalise_sponsor(message.text or "")
        try:
            await message.delete()
        except Exception:
            pass
        if target is None:
            await message.answer("Укажите публичный @username или ссылку t.me на канал/группу.")
            return
        loaded = await _load(deal_id)
        if loaded is None or message.from_user.id != loaded[0].buyer_user_id or loaded[0].status != "accepted":
            await state.clear()
            await message.answer("Эта сделка больше не принимает материалы.")
            return
        try:
            chat = await bot.get_chat(target)
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
        except Exception:
            await message.answer("Не удалось открыть этот канал/группу. Проверьте ссылку и добавьте Mimorus туда администратором.")
            return
        if chat.type not in {"channel", "group", "supergroup"} or member.status not in {"administrator", "creator"}:
            await message.answer("Mimorus должен быть администратором указанного канала или группы. После выдачи прав отправьте ссылку ещё раз.")
            return
        async with session_factory() as session:
            async with session.begin():
                placement = (await session.execute(select(AdvertisingPlacement).where(
                    AdvertisingPlacement.deal_id == deal_id,
                    AdvertisingPlacement.kind == "mandatory",
                ).with_for_update())).scalar_one_or_none()
                if placement is None:
                    await state.clear()
                    await message.answer("Размещение ОП не найдено.")
                    return
                cfg = dict(placement.config_json or {})
                cfg.update({
                    "sponsor_chat_id": chat.id,
                    "sponsor_title": chat.title or target,
                    "sponsor_username": getattr(chat, "username", None),
                })
                placement.config_json = cfg
                placement.status = "ready"
        await state.clear()
        await message.answer(
            f"✅ Спонсор сохранён: <b>{chat.title or target}</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Материалы сделки", callback_data=f"ads:materials:{deal_id}")]
            ]),
        )
        await _notify_if_ready(bot, deal_id)

    return router
