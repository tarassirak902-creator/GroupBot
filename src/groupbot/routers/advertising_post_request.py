from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement


class AdvertisingPostDraftState(StatesGroup):
    waiting_initial = State()
    waiting_text = State()
    waiting_photo = State()
    waiting_button_text = State()
    waiting_button_url = State()


def _editor_keyboard(deal_id: int, *, has_photo: bool, has_button: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"ads:post:text:{deal_id}"),
            InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"ads:post:photo:{deal_id}"),
        ],
        [InlineKeyboardButton(text="🔘 Изменить кнопку", callback_data=f"ads:post:button:{deal_id}")],
    ]
    if has_photo or has_button:
        extra: list[InlineKeyboardButton] = []
        if has_photo:
            extra.append(InlineKeyboardButton(text="🗑 Фото", callback_data=f"ads:post:remove_photo:{deal_id}"))
        if has_button:
            extra.append(InlineKeyboardButton(text="🗑 Кнопку", callback_data=f"ads:post:remove_button:{deal_id}"))
        rows.append(extra)
    rows.append([InlineKeyboardButton(text="✅ Подтвердить и отправить", callback_data=f"ads:post:submit:{deal_id}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"ads:post:cancel:{deal_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _seller_keyboard(deal_id: int, buyer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Посмотреть пост", callback_data=f"ads:post:view:{deal_id}")],
        [InlineKeyboardButton(text="💬 Связаться с покупателем", url=f"tg://user?id={buyer_id}")],
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ads:deal:accept:{deal_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:deal:reject:{deal_id}"),
        ],
    ])


def _preview_markup(deal_id: int, cfg: dict, *, seller: bool = False, buyer_id: int | None = None) -> InlineKeyboardMarkup:
    if seller and buyer_id is not None:
        return _seller_keyboard(deal_id, buyer_id)
    return _editor_keyboard(
        deal_id,
        has_photo=bool(cfg.get("photo_file_id")),
        has_button=bool(cfg.get("button_text") and cfg.get("button_url")),
    )


def _post_button(cfg: dict) -> InlineKeyboardMarkup | None:
    text = str(cfg.get("button_text") or "").strip()
    url = str(cfg.get("button_url") or "").strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text[:64], url=url)]])


async def _send_preview(bot: Bot, chat_id: int, deal_id: int, cfg: dict, *, seller: bool = False, buyer_id: int | None = None) -> None:
    text = str(cfg.get("text") or "")
    photo = cfg.get("photo_file_id")
    controls = _preview_markup(deal_id, cfg, seller=seller, buyer_id=buyer_id)
    if photo:
        await bot.send_photo(chat_id, photo=photo, caption=text or None, reply_markup=controls)
    else:
        await bot.send_message(chat_id, text or "(без текста)", reply_markup=controls)


def create_advertising_post_request_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="advertising_post_request")

    async def _load(deal_id: int):
        async with session_factory() as session:
            row = (await session.execute(
                select(AdvertisingDeal, AdvertisingListing, AdvertisingPlacement)
                .join(AdvertisingListing, AdvertisingListing.id == AdvertisingDeal.listing_id)
                .join(AdvertisingPlacement, (AdvertisingPlacement.deal_id == AdvertisingDeal.id) & (AdvertisingPlacement.kind == "post"))
                .where(AdvertisingDeal.id == deal_id)
            )).first()
            return row

    async def _save_cfg(deal_id: int, buyer_id: int, changes: dict) -> dict | None:
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.buyer_user_id != buyer_id or deal.status != "draft_post":
                    return None
                placement = (await session.execute(select(AdvertisingPlacement).where(
                    AdvertisingPlacement.deal_id == deal_id,
                    AdvertisingPlacement.kind == "post",
                ).with_for_update())).scalar_one_or_none()
                if placement is None:
                    return None
                cfg = dict(placement.config_json or {})
                cfg.update(changes)
                placement.config_json = cfg
                return cfg

    @router.callback_query(F.data.regexp(r"^ads:req:type:\d+:(post|both)$"))
    async def start_post_request(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        listing_id = int(parts[3])
        kind = parts[4]
        async with session_factory() as session:
            async with session.begin():
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == listing_id).with_for_update())).scalar_one_or_none()
                if listing is None or not listing.is_active or listing.owner_user_id == callback.from_user.id or not listing.offers_post:
                    await callback.answer("Объявление недоступно.", show_alert=True)
                    return
                if kind == "both" and not listing.offers_mandatory:
                    await callback.answer("ОП в этом объявлении больше не продаётся.", show_alert=True)
                    return
                existing = (await session.execute(select(AdvertisingDeal).where(
                    AdvertisingDeal.listing_id == listing_id,
                    AdvertisingDeal.buyer_user_id == callback.from_user.id,
                    AdvertisingDeal.status.in_(["draft_post", "pending"]),
                ).with_for_update())).scalar_one_or_none()
                if existing is not None:
                    deal = existing
                    placement = (await session.execute(select(AdvertisingPlacement).where(
                        AdvertisingPlacement.deal_id == deal.id,
                        AdvertisingPlacement.kind == "post",
                    ))).scalar_one_or_none()
                    if placement is None:
                        placement = AdvertisingPlacement(deal_id=deal.id, kind="post", status="draft", config_json={})
                        session.add(placement)
                else:
                    deal = AdvertisingDeal(
                        listing_id=listing.id,
                        seller_user_id=listing.owner_user_id,
                        buyer_user_id=callback.from_user.id,
                        requested_post=True,
                        requested_mandatory=(kind == "both"),
                        status="draft_post",
                        agreed_terms_json={
                            "post_price_stars": listing.post_price_stars,
                            "post_interval_minutes": listing.post_interval_minutes,
                            "post_terms": listing.post_terms_json,
                            "mandatory_price_stars": listing.mandatory_price_stars if kind == "both" else None,
                            "mandatory_terms": listing.mandatory_terms_json if kind == "both" else None,
                        },
                    )
                    session.add(deal)
                    await session.flush()
                    placement = AdvertisingPlacement(
                        deal_id=deal.id,
                        kind="post",
                        status="draft",
                        config_json={
                            "price_stars": listing.post_price_stars,
                            "interval_minutes": listing.post_interval_minutes,
                            "terms": listing.post_terms_json,
                        },
                    )
                    session.add(placement)
                deal_id = deal.id
        await state.set_state(AdvertisingPostDraftState.waiting_initial)
        await state.update_data(advertising_post_deal_id=deal_id)
        await callback.message.edit_text(
            "📣 <b>Рекламный пост</b>\n\n"
            "Пришлите пост, который хотите рекламировать. Можно отправить текст или фото с подписью.\n\n"
            "После этого Mimorus покажет предпросмотр и даст изменить текст, фото и кнопку перед отправкой рекламодателю.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data=f"ads:post:cancel:{deal_id}")]]),
        )
        await callback.answer()

    @router.message(AdvertisingPostDraftState.waiting_initial, F.chat.type == "private")
    async def initial_post(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        deal_id = data.get("advertising_post_deal_id")
        if not isinstance(deal_id, int) or message.from_user is None:
            return
        changes: dict = {}
        if message.photo:
            changes["photo_file_id"] = message.photo[-1].file_id
            changes["text"] = message.caption or ""
        elif message.text:
            changes["text"] = message.text
            changes["photo_file_id"] = None
        else:
            await message.answer("Сейчас для рекламного поста поддерживаются текст или фото с подписью.")
            return
        cfg = await _save_cfg(deal_id, message.from_user.id, changes)
        if cfg is None:
            await state.clear()
            await message.answer("Черновик больше недоступен.")
            return
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await message.answer("👁 <b>Предпросмотр рекламного поста</b>", parse_mode="HTML")
        await _send_preview(bot, message.chat.id, deal_id, cfg)

    @router.callback_query(F.data.regexp(r"^ads:post:text:\d+$"))
    async def edit_text(callback: CallbackQuery, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        row = await _load(deal_id)
        if row is None or row[0].buyer_user_id != callback.from_user.id or row[0].status != "draft_post":
            await callback.answer("Черновик недоступен.", show_alert=True)
            return
        await state.set_state(AdvertisingPostDraftState.waiting_text)
        await state.update_data(advertising_post_deal_id=deal_id)
        await callback.message.answer("✏️ Отправьте новый текст рекламного поста. Для пустого текста отправьте один символ «-».")
        await callback.answer()

    @router.message(AdvertisingPostDraftState.waiting_text, F.chat.type == "private")
    async def save_text(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.text is None:
            return
        deal_id = (await state.get_data()).get("advertising_post_deal_id")
        if not isinstance(deal_id, int):
            return
        cfg = await _save_cfg(deal_id, message.from_user.id, {"text": "" if message.text.strip() == "-" else message.text})
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        if cfg is not None:
            await message.answer("👁 <b>Обновлённый предпросмотр</b>", parse_mode="HTML")
            await _send_preview(bot, message.chat.id, deal_id, cfg)

    @router.callback_query(F.data.regexp(r"^ads:post:photo:\d+$"))
    async def edit_photo(callback: CallbackQuery, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        row = await _load(deal_id)
        if row is None or row[0].buyer_user_id != callback.from_user.id or row[0].status != "draft_post":
            await callback.answer("Черновик недоступен.", show_alert=True)
            return
        await state.set_state(AdvertisingPostDraftState.waiting_photo)
        await state.update_data(advertising_post_deal_id=deal_id)
        await callback.message.answer("🖼 Отправьте новое фото для рекламного поста.")
        await callback.answer()

    @router.message(AdvertisingPostDraftState.waiting_photo, F.chat.type == "private", F.photo)
    async def save_photo(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not message.photo:
            return
        deal_id = (await state.get_data()).get("advertising_post_deal_id")
        if not isinstance(deal_id, int):
            return
        cfg = await _save_cfg(deal_id, message.from_user.id, {"photo_file_id": message.photo[-1].file_id})
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        if cfg is not None:
            await message.answer("👁 <b>Обновлённый предпросмотр</b>", parse_mode="HTML")
            await _send_preview(bot, message.chat.id, deal_id, cfg)

    @router.callback_query(F.data.regexp(r"^ads:post:button:\d+$"))
    async def edit_button(callback: CallbackQuery, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        row = await _load(deal_id)
        if row is None or row[0].buyer_user_id != callback.from_user.id or row[0].status != "draft_post":
            await callback.answer("Черновик недоступен.", show_alert=True)
            return
        await state.set_state(AdvertisingPostDraftState.waiting_button_text)
        await state.update_data(advertising_post_deal_id=deal_id)
        await callback.message.answer("🔘 Отправьте текст кнопки, например: Перейти в канал")
        await callback.answer()

    @router.message(AdvertisingPostDraftState.waiting_button_text, F.chat.type == "private")
    async def button_text(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not message.text:
            return
        await state.update_data(advertising_post_button_text=message.text.strip()[:64])
        await state.set_state(AdvertisingPostDraftState.waiting_button_url)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔗 Теперь отправьте ссылку кнопки, например https://t.me/example")

    @router.message(AdvertisingPostDraftState.waiting_button_url, F.chat.type == "private")
    async def button_url(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not message.text:
            return
        url = message.text.strip()
        if not url.startswith(("https://", "http://", "tg://")):
            await message.answer("Ссылка должна начинаться с https://, http:// или tg://")
            return
        data = await state.get_data()
        deal_id = data.get("advertising_post_deal_id")
        text = data.get("advertising_post_button_text")
        if not isinstance(deal_id, int) or not isinstance(text, str):
            await state.clear()
            return
        cfg = await _save_cfg(deal_id, message.from_user.id, {"button_text": text, "button_url": url})
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        if cfg is not None:
            await message.answer("👁 <b>Обновлённый предпросмотр</b>", parse_mode="HTML")
            await _send_preview(bot, message.chat.id, deal_id, cfg)

    @router.callback_query(F.data.regexp(r"^ads:post:remove_(photo|button):\d+$"))
    async def remove_part(callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        action = parts[2]
        deal_id = int(parts[3])
        changes = {"photo_file_id": None} if action == "remove_photo" else {"button_text": None, "button_url": None}
        cfg = await _save_cfg(deal_id, callback.from_user.id, changes)
        if cfg is None:
            await callback.answer("Черновик недоступен.", show_alert=True)
            return
        await callback.message.answer("👁 <b>Обновлённый предпросмотр</b>", parse_mode="HTML")
        await _send_preview(bot, callback.from_user.id, deal_id, cfg)
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:post:submit:\d+$"))
    async def submit(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        seller_id = None
        title = ""
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is None or deal.buyer_user_id != callback.from_user.id or deal.status != "draft_post":
                    await callback.answer("Черновик недоступен.", show_alert=True)
                    return
                listing = (await session.execute(select(AdvertisingListing).where(AdvertisingListing.id == deal.listing_id))).scalar_one_or_none()
                placement = (await session.execute(select(AdvertisingPlacement).where(
                    AdvertisingPlacement.deal_id == deal.id,
                    AdvertisingPlacement.kind == "post",
                ).with_for_update())).scalar_one_or_none()
                if listing is None or placement is None:
                    await callback.answer("Не удалось подготовить заявку.", show_alert=True)
                    return
                cfg = dict(placement.config_json or {})
                if not str(cfg.get("text") or "").strip() and not cfg.get("photo_file_id"):
                    await callback.answer("Пост должен содержать текст или фото.", show_alert=True)
                    return
                deal.status = "pending"
                placement.status = "ready"
                seller_id = deal.seller_user_id
                title = listing.group_title_snapshot
        await state.clear()
        await callback.message.answer(
            "✅ <b>Рекламный пост отправлен рекламодателю на рассмотрение</b>\n\n"
            f"🏠 Площадка: <b>{title}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")]]),
        )
        if seller_id is not None:
            try:
                await bot.send_message(
                    seller_id,
                    "📥 <b>Новая заявка на рекламный пост</b>\n\n"
                    f"🏠 Площадка: <b>{title}</b>\n"
                    "Покупатель уже подготовил пост. Посмотрите его и примите решение.",
                    parse_mode="HTML",
                    reply_markup=_seller_keyboard(deal_id, callback.from_user.id),
                )
            except Exception:
                pass
        await callback.answer("Отправлено рекламодателю")

    @router.callback_query(F.data.regexp(r"^ads:post:view:\d+$"))
    async def seller_view(callback: CallbackQuery, bot: Bot) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        row = await _load(deal_id)
        if row is None or row[0].seller_user_id != callback.from_user.id:
            await callback.answer("Пост недоступен.", show_alert=True)
            return
        cfg = dict(row[2].config_json or {})
        await callback.message.answer("👁 <b>Рекламный пост покупателя</b>", parse_mode="HTML")
        await _send_preview(bot, callback.from_user.id, deal_id, cfg, seller=True, buyer_id=row[0].buyer_user_id)
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:post:cancel:\d+$"))
    async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
        deal_id = int((callback.data or "").rsplit(":", 1)[1])
        async with session_factory() as session:
            async with session.begin():
                deal = (await session.execute(select(AdvertisingDeal).where(AdvertisingDeal.id == deal_id).with_for_update())).scalar_one_or_none()
                if deal is not None and deal.buyer_user_id == callback.from_user.id and deal.status == "draft_post":
                    deal.status = "cancelled"
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text("❌ Создание рекламного поста отменено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")]]))
        await callback.answer()

    return router
