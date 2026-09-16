from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.config import Settings
from groupbot.models import Group, User
from groupbot.services.users import upsert_user
from groupbot.support_models import SupportMessage, SupportTicket


class SupportState(StatesGroup):
    waiting_ticket_text = State()
    waiting_creator_reply = State()
    waiting_creator_direct = State()


def _user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="support:new:question")],
        [InlineKeyboardButton(text="💡 Предложить идею", callback_data="support:new:suggestion")],
        [InlineKeyboardButton(text="📋 Мои обращения", callback_data="support:mine")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
    ])


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="support:home")]])


def _creator_menu(open_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔴 Открытые ({open_count})", callback_data="support:creator:list:open")],
        [InlineKeyboardButton(text="✅ Закрытые", callback_data="support:creator:list:closed")],
        [InlineKeyboardButton(text="🗑 Удалённые", callback_data="support:creator:list:deleted")],
        [InlineKeyboardButton(text="◀️ Панель создателя", callback_data="creator:home")],
    ])


def _ticket_keyboard(ticket: SupportTicket) -> InlineKeyboardMarkup:
    rows = []
    if ticket.deleted_at is None:
        rows += [
            [InlineKeyboardButton(text="💬 Ответить", callback_data=f"support:creator:reply:{ticket.id}"), InlineKeyboardButton(text="✉️ Написать пользователю", callback_data=f"support:creator:direct:{ticket.id}")],
        ]
        if ticket.status == "open":
            rows.append([InlineKeyboardButton(text="✅ Закрыть", callback_data=f"support:creator:close:{ticket.id}")])
        else:
            rows.append([InlineKeyboardButton(text="🔴 Открыть снова", callback_data=f"support:creator:reopen:{ticket.id}")])
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"support:creator:delete:{ticket.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К обращениям", callback_data="support:creator")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kind(kind: str) -> str:
    return "💡 Предложение" if kind == "suggestion" else "❓ Вопрос"


def _status(ticket: SupportTicket) -> str:
    if ticket.deleted_at is not None:
        return "🗑 Удалено"
    return "🔴 Открыто" if ticket.status == "open" else "✅ Закрыто"


async def _ticket_text(session: AsyncSession, ticket: SupportTicket) -> str:
    user = (await session.execute(select(User).where(User.telegram_user_id == ticket.user_id))).scalar_one_or_none()
    messages = list((await session.execute(select(SupportMessage).where(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.id))).scalars().all())
    name = escape((getattr(user, "full_name", None) or getattr(user, "username", None) or str(ticket.user_id)))
    username = getattr(user, "username", None)
    who = f'<a href="tg://user?id={ticket.user_id}">{name}</a>'
    if username:
        who += f" (@{escape(username)})"
    history = []
    for item in messages[-12:]:
        role = "👤 Пользователь" if item.sender_role == "user" else "🛠 Поддержка"
        history.append(f"<b>{role}:</b>\n{escape(item.text)}")
    history_text = "\n\n".join(history) if history else "—"
    created = ticket.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return (
        f"🛠 <b>Обращение #{ticket.id}</b>\n\n"
        f"{_kind(ticket.kind)}\n"
        f"👤 От: {who}\n"
        f"🆔 <code>{ticket.user_id}</code>\n"
        f"📍 Источник: <b>{escape(ticket.source_label)}</b>\n"
        f"🕒 {created}\n"
        f"Статус: {_status(ticket)}\n\n"
        f"{history_text}"
    )


def create_support_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> Router:
    router = Router(name="support")

    def is_creator(user_id: int) -> bool:
        return user_id in settings.creator_id_set

    async def creator_home(message: Message) -> None:
        async with session_factory() as s:
            count = (await s.execute(select(func.count(SupportTicket.id)).where(SupportTicket.status == "open", SupportTicket.deleted_at.is_(None)))).scalar_one()
        await message.edit_text("🛠 <b>Поддержка Mimorus</b>\n\nЗдесь находятся вопросы и предложения пользователей.", parse_mode="HTML", reply_markup=_creator_menu(count))

    @router.message(F.chat.type == "private", F.text == "🛠 Поддержка")
    async def support_home_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("🛠 <b>Поддержка Mimorus</b>\n\nВыберите, что хотите отправить. Ответ придёт сюда в ЛС.", parse_mode="HTML", reply_markup=_user_menu())

    @router.callback_query(F.data == "support:home")
    async def support_home_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if callback.message:
            await callback.message.edit_text("🛠 <b>Поддержка Mimorus</b>\n\nВыберите, что хотите отправить. Ответ придёт сюда в ЛС.", parse_mode="HTML", reply_markup=_user_menu())
        await callback.answer()

    @router.callback_query(F.data.startswith("support:new:"))
    async def new_ticket(callback: CallbackQuery, state: FSMContext) -> None:
        kind = (callback.data or "").rsplit(":", 1)[-1]
        if kind not in {"question", "suggestion"}:
            return
        await state.set_state(SupportState.waiting_ticket_text)
        await state.update_data(kind=kind)
        if callback.message:
            await callback.message.edit_text(f"{_kind(kind)}\n\nНапишите сообщение одним сообщением. Оно будет передано создателю Mimorus.", reply_markup=_cancel_keyboard())
        await callback.answer()

    @router.message(SupportState.waiting_ticket_text, F.chat.type == "private")
    async def save_ticket(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not message.text or not message.text.strip():
            await message.answer("Отправьте текст обращения одним сообщением.")
            return
        data = await state.get_data(); kind = data.get("kind", "question")
        async with session_factory() as s:
            async with s.begin():
                await upsert_user(s, message.from_user)
                ticket = SupportTicket(user_id=message.from_user.id, kind=kind, source_label="ЛС с ботом")
                s.add(ticket); await s.flush()
                s.add(SupportMessage(ticket_id=ticket.id, sender_user_id=message.from_user.id, sender_role="user", text=message.text.strip()))
                ticket_id = ticket.id
        await state.clear()
        await message.answer(f"✅ <b>Обращение #{ticket_id} создано.</b>\n\nОтвет поддержки придёт сюда в ЛС.", parse_mode="HTML", reply_markup=_user_menu())
        for creator_id in settings.creator_id_set:
            try:
                await bot.send_message(creator_id, f"🔔 <b>Новое обращение #{ticket_id}</b>\n\n{_kind(kind)}\n👤 {escape(message.from_user.full_name)}\n📍 ЛС с ботом", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Открыть обращение", callback_data=f"support:creator:ticket:{ticket_id}")]]))
            except Exception:
                pass

    @router.callback_query(F.data == "support:mine")
    async def my_tickets(callback: CallbackQuery) -> None:
        async with session_factory() as s:
            tickets = list((await s.execute(select(SupportTicket).where(SupportTicket.user_id == callback.from_user.id, SupportTicket.deleted_at.is_(None)).order_by(SupportTicket.id.desc()).limit(20))).scalars().all())
        rows = [[InlineKeyboardButton(text=f"{_status(t)} • #{t.id} • {'Идея' if t.kind == 'suggestion' else 'Вопрос'}", callback_data=f"support:mine:ticket:{t.id}")] for t in tickets]
        rows.append([InlineKeyboardButton(text="◀️ Поддержка", callback_data="support:home")])
        if callback.message:
            await callback.message.edit_text("📋 <b>Мои обращения</b>\n\n" + ("Выберите обращение:" if tickets else "У вас пока нет обращений."), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("support:mine:ticket:"))
    async def my_ticket(callback: CallbackQuery) -> None:
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1])
        async with session_factory() as s:
            ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id, SupportTicket.user_id == callback.from_user.id, SupportTicket.deleted_at.is_(None)))).scalar_one_or_none()
            if ticket is None:
                await callback.answer("Обращение не найдено.", show_alert=True); return
            text = await _ticket_text(s, ticket)
        if callback.message:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Мои обращения", callback_data="support:mine")]]))
        await callback.answer()

    @router.callback_query(F.data.in_({"support:creator", "creator:section:support"}))
    async def creator_support(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_creator(callback.from_user.id):
            await callback.answer("Недоступно.", show_alert=True); return
        await state.clear()
        if callback.message:
            await creator_home(callback.message)
        await callback.answer()

    @router.callback_query(F.data.startswith("support:creator:list:"))
    async def creator_list(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id): return
        mode = (callback.data or "").rsplit(":", 1)[-1]
        async with session_factory() as s:
            stmt = select(SupportTicket).order_by(SupportTicket.id.desc()).limit(30)
            if mode == "deleted": stmt = stmt.where(SupportTicket.deleted_at.is_not(None))
            else: stmt = stmt.where(SupportTicket.deleted_at.is_(None), SupportTicket.status == mode)
            tickets = list((await s.execute(stmt)).scalars().all())
        rows = [[InlineKeyboardButton(text=f"#{t.id} • {'💡' if t.kind == 'suggestion' else '❓'} {t.source_label}"[:64], callback_data=f"support:creator:ticket:{t.id}")] for t in tickets]
        rows.append([InlineKeyboardButton(text="◀️ Поддержка", callback_data="support:creator")])
        if callback.message:
            await callback.message.edit_text("📋 <b>Обращения</b>\n\n" + ("Выберите обращение:" if tickets else "Список пуст."), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("support:creator:ticket:"))
    async def creator_ticket(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id): return
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1])
        async with session_factory() as s:
            ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
            if ticket is None: await callback.answer("Обращение не найдено.", show_alert=True); return
            text = await _ticket_text(s, ticket)
        if callback.message: await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_ticket_keyboard(ticket))
        await callback.answer()

    @router.callback_query(F.data.startswith("support:creator:reply:"))
    async def creator_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_creator(callback.from_user.id): return
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1])
        await state.set_state(SupportState.waiting_creator_reply); await state.update_data(ticket_id=ticket_id)
        if callback.message: await callback.message.edit_text(f"💬 Ответ на обращение #{ticket_id}\n\nНапишите ответ одним сообщением.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"support:creator:ticket:{ticket_id}")]]))
        await callback.answer()

    @router.message(SupportState.waiting_creator_reply, F.chat.type == "private")
    async def creator_reply_send(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not is_creator(message.from_user.id) or not message.text: return
        ticket_id = int((await state.get_data())["ticket_id"])
        async with session_factory() as s:
            async with s.begin():
                ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
                if ticket is None: await state.clear(); return
                s.add(SupportMessage(ticket_id=ticket.id, sender_user_id=message.from_user.id, sender_role="creator", text=message.text.strip()))
                user_id = ticket.user_id
        await state.clear()
        try: await bot.send_message(user_id, f"🛠 <b>Ответ поддержки</b>\nОбращение #{ticket_id}\n\n{escape(message.text.strip())}", parse_mode="HTML")
        except Exception: pass
        await message.answer(f"✅ Ответ по обращению #{ticket_id} отправлен пользователю.")

    @router.callback_query(F.data.startswith("support:creator:direct:"))
    async def creator_direct_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_creator(callback.from_user.id): return
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1])
        await state.set_state(SupportState.waiting_creator_direct); await state.update_data(ticket_id=ticket_id)
        if callback.message: await callback.message.edit_text(f"✉️ Сообщение пользователю из обращения #{ticket_id}\n\nОно не будет добавлено в историю обращения.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"support:creator:ticket:{ticket_id}")]]))
        await callback.answer()

    @router.message(SupportState.waiting_creator_direct, F.chat.type == "private")
    async def creator_direct_send(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not is_creator(message.from_user.id) or not message.text: return
        ticket_id = int((await state.get_data())["ticket_id"])
        async with session_factory() as s:
            ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
        await state.clear()
        if ticket is None: return
        try: await bot.send_message(ticket.user_id, f"✉️ <b>Сообщение от Mimorus</b>\n\n{escape(message.text.strip())}", parse_mode="HTML")
        except Exception: pass
        await message.answer("✅ Сообщение отправлено пользователю.")

    @router.callback_query(F.data.startswith("support:creator:close:"))
    async def creator_close(callback: CallbackQuery, bot: Bot) -> None:
        if not is_creator(callback.from_user.id): return
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1]); user_id = None
        async with session_factory() as s:
            async with s.begin():
                ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
                if ticket: ticket.status = "closed"; ticket.closed_at = datetime.now(timezone.utc); user_id = ticket.user_id
        if user_id:
            try: await bot.send_message(user_id, f"✅ Обращение #{ticket_id} закрыто поддержкой Mimorus.")
            except Exception: pass
        await callback.answer("Обращение закрыто.")
        if callback.message: await creator_home(callback.message)

    @router.callback_query(F.data.startswith("support:creator:reopen:"))
    async def creator_reopen(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id): return
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1])
        async with session_factory() as s:
            async with s.begin():
                ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
                if ticket: ticket.status = "open"; ticket.closed_at = None
        await callback.answer("Обращение снова открыто.")
        if callback.message: await creator_home(callback.message)

    @router.callback_query(F.data.startswith("support:creator:delete:"))
    async def creator_delete(callback: CallbackQuery) -> None:
        if not is_creator(callback.from_user.id): return
        ticket_id = int((callback.data or "0").rsplit(":", 1)[-1])
        async with session_factory() as s:
            async with s.begin():
                ticket = (await s.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
                if ticket: ticket.deleted_at = datetime.now(timezone.utc)
        await callback.answer("Обращение удалено из рабочих списков.")
        if callback.message: await creator_home(callback.message)

    return router
