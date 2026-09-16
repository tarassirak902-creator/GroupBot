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
from groupbot.models import Group, GroupOwner, User
from groupbot.services.users import upsert_user
from groupbot.support_models import SupportMessage, SupportTicket

class SupportState(StatesGroup):
    waiting_ticket_text = State(); waiting_creator_reply = State(); waiting_creator_direct = State()

def _user_menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❓ Задать вопрос", callback_data="support:new:question")],[InlineKeyboardButton(text="💡 Предложить идею", callback_data="support:new:suggestion")],[InlineKeyboardButton(text="📋 Мои обращения", callback_data="support:mine")],[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]])
def _new_menu(chat_id):
    suffix=f":{chat_id}" if chat_id is not None else ""; return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❓ Задать вопрос",callback_data=f"support:new:question{suffix}")],[InlineKeyboardButton(text="💡 Предложить идею",callback_data=f"support:new:suggestion{suffix}")],[InlineKeyboardButton(text="❌ Отмена",callback_data="support:home")]])
def _creator_menu(n):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🔴 Открытые ({n})",callback_data="support:creator:list:open")],[InlineKeyboardButton(text="✅ Закрытые",callback_data="support:creator:list:closed")],[InlineKeyboardButton(text="🗑 Удалённые",callback_data="support:creator:list:deleted")],[InlineKeyboardButton(text="◀️ Панель создателя",callback_data="creator:home")]])
def _kind(k): return "💡 Предложение" if k=="suggestion" else "❓ Вопрос"
def _status(t): return "🗑 Удалено" if t.deleted_at else ("🔴 Открыто" if t.status=="open" else "✅ Закрыто")
def _ticket_keyboard(t):
    rows=[]
    if t.deleted_at is None:
        rows.append([InlineKeyboardButton(text="💬 Ответить",callback_data=f"support:creator:reply:{t.id}"),InlineKeyboardButton(text="✉️ Написать пользователю",callback_data=f"support:creator:direct:{t.id}")]); rows.append([InlineKeyboardButton(text="✅ Закрыть" if t.status=="open" else "🔴 Открыть снова",callback_data=f"support:creator:{'close' if t.status=='open' else 'reopen'}:{t.id}")]); rows.append([InlineKeyboardButton(text="🗑 Удалить",callback_data=f"support:creator:delete:{t.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К обращениям",callback_data="support:creator")]); return InlineKeyboardMarkup(inline_keyboard=rows)
async def _ticket_text(s,t):
    u=(await s.execute(select(User).where(User.telegram_user_id==t.user_id))).scalar_one_or_none(); ms=list((await s.execute(select(SupportMessage).where(SupportMessage.ticket_id==t.id).order_by(SupportMessage.id))).scalars().all()); name=escape(" ".join(x for x in [getattr(u,"first_name",None),getattr(u,"last_name",None)] if x) or getattr(u,"username",None) or str(t.user_id)); un=getattr(u,"username",None); who=f'<a href="tg://user?id={t.user_id}">{name}</a>'+(f" (@{escape(un)})" if un else ""); hist="\n\n".join(f"<b>{'👤 Пользователь' if m.sender_role=='user' else '🛠 Поддержка'}:</b>\n{escape(m.text)}" for m in ms[-12:]) or "—"; created=t.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"); return f"🛠 <b>Обращение #{t.id}</b>\n\n{_kind(t.kind)}\n👤 От: {who}\n🆔 <code>{t.user_id}</code>\n📍 Источник: <b>{escape(t.source_label)}</b>\n🕒 {created}\nСтатус: {_status(t)}\n\n{hist}"

def create_support_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings)->Router:
    r=Router(name="support"); is_creator=lambda uid: uid in settings.creator_id_set
    async def creator_home(m):
        async with session_factory() as s: n=(await s.execute(select(func.count(SupportTicket.id)).where(SupportTicket.status=="open",SupportTicket.deleted_at.is_(None)))).scalar_one()
        await m.edit_text("🛠 <b>Поддержка Mimorus</b>\n\nЗдесь находятся вопросы и предложения пользователей.",parse_mode="HTML",reply_markup=_creator_menu(n))
    @r.message(F.chat.type=="private",F.text=="🛠 Поддержка")
    async def home_msg(m:Message,state:FSMContext): await state.clear(); await m.answer("🛠 <b>Поддержка Mimorus</b>\n\nВыберите, что хотите отправить. Ответ придёт сюда в ЛС.",parse_mode="HTML",reply_markup=_user_menu())
    @r.callback_query(F.data=="support:home")
    async def home_cb(c:CallbackQuery,state:FSMContext):
        await state.clear()
        if c.message: await c.message.edit_text("🛠 <b>Поддержка Mimorus</b>\n\nВыберите, что хотите отправить. Ответ придёт сюда в ЛС.",parse_mode="HTML",reply_markup=_user_menu())
        await c.answer()
    @r.callback_query(F.data.startswith("support:group:"))
    async def group_support(c:CallbackQuery,state:FSMContext):
        await state.clear()
        try: cid=int((c.data or "").rsplit(":",1)[-1])
        except ValueError: await c.answer("Некорректная группа.",show_alert=True); return
        async with session_factory() as s: g=(await s.execute(select(Group).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(Group.chat_id==cid,GroupOwner.user_id==c.from_user.id,GroupOwner.is_current.is_(True)))).scalar_one_or_none()
        if not g: await c.answer("Группа не принадлежит вашему аккаунту.",show_alert=True); return
        if c.message: await c.message.edit_text(f"🛠 <b>Поддержка по группе</b>\n\nГруппа: <b>{escape(g.title or str(g.chat_id))}</b>\n\nВыберите тип обращения.",parse_mode="HTML",reply_markup=_new_menu(cid))
        await c.answer()
    @r.callback_query(F.data.startswith("support:new:"))
    async def new(c:CallbackQuery,state:FSMContext):
        p=(c.data or "").split(":"); kind=p[2] if len(p)>2 else ""; cid=None
        if len(p)>3:
            try: cid=int(p[3])
            except ValueError: pass
        if kind not in {"question","suggestion"}: return
        label="ЛС с ботом"
        if cid is not None:
            async with session_factory() as s: g=(await s.execute(select(Group).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(Group.chat_id==cid,GroupOwner.user_id==c.from_user.id,GroupOwner.is_current.is_(True)))).scalar_one_or_none()
            if not g: await c.answer("Группа больше не доступна.",show_alert=True); return
            label=f"Группа: {g.title or g.chat_id}"
        await state.set_state(SupportState.waiting_ticket_text); await state.update_data(kind=kind,source_chat_id=cid,source_label=label)
        if c.message: await c.message.edit_text(f"{_kind(kind)}\n📍 {escape(label)}\n\nНапишите сообщение одним сообщением.",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена",callback_data="support:home")]]))
        await c.answer()
    @r.message(SupportState.waiting_ticket_text,F.chat.type=="private")
    async def save(m:Message,state:FSMContext,bot:Bot):
        if not m.from_user or not m.text or not m.text.strip(): await m.answer("Отправьте текст обращения одним сообщением."); return
        d=await state.get_data(); kind=d.get("kind","question"); cid=d.get("source_chat_id"); label=d.get("source_label","ЛС с ботом")
        async with session_factory() as s:
            async with s.begin(): await upsert_user(s,m.from_user); t=SupportTicket(user_id=m.from_user.id,kind=kind,source_chat_id=cid,source_label=label); s.add(t); await s.flush(); s.add(SupportMessage(ticket_id=t.id,sender_user_id=m.from_user.id,sender_role="user",text=m.text.strip())); tid=t.id
        await state.clear(); await m.answer(f"✅ <b>Обращение #{tid} создано.</b>\n\nОтвет поддержки придёт сюда в ЛС.",parse_mode="HTML",reply_markup=_user_menu())
        for creator in settings.creator_id_set:
            try: await bot.send_message(creator,f"🔔 <b>Новое обращение #{tid}</b>\n\n{_kind(kind)}\n👤 {escape(m.from_user.full_name)}\n📍 {escape(label)}",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Открыть обращение",callback_data=f"support:creator:ticket:{tid}")]]))
            except Exception: pass
    @r.callback_query(F.data=="support:mine")
    async def mine(c:CallbackQuery):
        async with session_factory() as s: ts=list((await s.execute(select(SupportTicket).where(SupportTicket.user_id==c.from_user.id,SupportTicket.deleted_at.is_(None)).order_by(SupportTicket.id.desc()).limit(20))).scalars().all())
        rows=[[InlineKeyboardButton(text=f"{_status(t)} • #{t.id} • {'Идея' if t.kind=='suggestion' else 'Вопрос'}",callback_data=f"support:mine:ticket:{t.id}")] for t in ts]+[[InlineKeyboardButton(text="◀️ Поддержка",callback_data="support:home")]]
        if c.message: await c.message.edit_text("📋 <b>Мои обращения</b>\n\n"+("Выберите обращение:" if ts else "У вас пока нет обращений."),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await c.answer()
    @r.callback_query(F.data.startswith("support:mine:ticket:"))
    async def mine_ticket(c:CallbackQuery):
        tid=int((c.data or "0").rsplit(":",1)[-1])
        async with session_factory() as s:
            t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid,SupportTicket.user_id==c.from_user.id,SupportTicket.deleted_at.is_(None)))).scalar_one_or_none()
            if not t: await c.answer("Обращение не найдено.",show_alert=True); return
            text=await _ticket_text(s,t)
        if c.message: await c.message.edit_text(text,parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Мои обращения",callback_data="support:mine")]]))
        await c.answer()
    @r.callback_query(F.data.in_({"support:creator","creator:section:support"}))
    async def cr_home(c:CallbackQuery,state:FSMContext):
        if not is_creator(c.from_user.id): await c.answer("Недоступно.",show_alert=True); return
        await state.clear()
        if c.message: await creator_home(c.message)
        await c.answer()
    @r.callback_query(F.data.startswith("support:creator:list:"))
    async def cr_list(c:CallbackQuery):
        if not is_creator(c.from_user.id): return
        mode=(c.data or "").rsplit(":",1)[-1]
        async with session_factory() as s:
            q=select(SupportTicket).order_by(SupportTicket.id.desc()).limit(30); q=q.where(SupportTicket.deleted_at.is_not(None)) if mode=="deleted" else q.where(SupportTicket.deleted_at.is_(None),SupportTicket.status==mode); ts=list((await s.execute(q)).scalars().all())
        rows=[[InlineKeyboardButton(text=f"#{t.id} • {'💡' if t.kind=='suggestion' else '❓'} {t.source_label}"[:64],callback_data=f"support:creator:ticket:{t.id}")] for t in ts]+[[InlineKeyboardButton(text="◀️ Поддержка",callback_data="support:creator")]]
        if c.message: await c.message.edit_text("📋 <b>Обращения</b>\n\n"+("Выберите обращение:" if ts else "Список пуст."),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await c.answer()
    @r.callback_query(F.data.startswith("support:creator:ticket:"))
    async def cr_ticket(c:CallbackQuery):
        if not is_creator(c.from_user.id): return
        tid=int((c.data or "0").rsplit(":",1)[-1])
        async with session_factory() as s:
            t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid))).scalar_one_or_none()
            if not t: await c.answer("Обращение не найдено.",show_alert=True); return
            text=await _ticket_text(s,t)
        if c.message: await c.message.edit_text(text,parse_mode="HTML",reply_markup=_ticket_keyboard(t))
        await c.answer()
    @r.callback_query(F.data.startswith("support:creator:reply:"))
    async def reply_start(c:CallbackQuery,state:FSMContext):
        if not is_creator(c.from_user.id): return
        tid=int((c.data or "0").rsplit(":",1)[-1]); await state.set_state(SupportState.waiting_creator_reply); await state.update_data(ticket_id=tid)
        if c.message: await c.message.edit_text(f"💬 Ответ на обращение #{tid}\n\nНапишите ответ одним сообщением.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена",callback_data=f"support:creator:ticket:{tid}")]]))
        await c.answer()
    @r.message(SupportState.waiting_creator_reply,F.chat.type=="private")
    async def reply_send(m:Message,state:FSMContext,bot:Bot):
        if not m.from_user or not is_creator(m.from_user.id) or not m.text: return
        tid=int((await state.get_data())["ticket_id"])
        async with session_factory() as s:
            async with s.begin():
                t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid))).scalar_one_or_none()
                if not t: await state.clear(); return
                s.add(SupportMessage(ticket_id=t.id,sender_user_id=m.from_user.id,sender_role="creator",text=m.text.strip())); uid=t.user_id
        await state.clear()
        try: await bot.send_message(uid,f"🛠 <b>Ответ поддержки</b>\nОбращение #{tid}\n\n{escape(m.text.strip())}",parse_mode="HTML")
        except Exception: pass
        await m.answer(f"✅ Ответ по обращению #{tid} отправлен пользователю.")
    @r.callback_query(F.data.startswith("support:creator:direct:"))
    async def direct_start(c:CallbackQuery,state:FSMContext):
        if not is_creator(c.from_user.id): return
        tid=int((c.data or "0").rsplit(":",1)[-1]); await state.set_state(SupportState.waiting_creator_direct); await state.update_data(ticket_id=tid)
        if c.message: await c.message.edit_text(f"✉️ Сообщение пользователю из обращения #{tid}\n\nОно не будет добавлено в историю обращения.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена",callback_data=f"support:creator:ticket:{tid}")]]))
        await c.answer()
    @r.message(SupportState.waiting_creator_direct,F.chat.type=="private")
    async def direct_send(m:Message,state:FSMContext,bot:Bot):
        if not m.from_user or not is_creator(m.from_user.id) or not m.text: return
        tid=int((await state.get_data())["ticket_id"])
        async with session_factory() as s: t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid))).scalar_one_or_none()
        await state.clear()
        if not t: return
        try: await bot.send_message(t.user_id,f"✉️ <b>Сообщение от Mimorus</b>\n\n{escape(m.text.strip())}",parse_mode="HTML")
        except Exception: pass
        await m.answer("✅ Сообщение отправлено пользователю.")
    @r.callback_query(F.data.startswith("support:creator:close:"))
    async def close(c:CallbackQuery,bot:Bot):
        if not is_creator(c.from_user.id): return
        tid=int((c.data or "0").rsplit(":",1)[-1]); uid=None
        async with session_factory() as s:
            async with s.begin():
                t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid))).scalar_one_or_none()
                if t: t.status="closed"; t.closed_at=datetime.now(timezone.utc); uid=t.user_id
        if uid:
            try: await bot.send_message(uid,f"✅ Обращение #{tid} закрыто поддержкой Mimorus.")
            except Exception: pass
        await c.answer("Обращение закрыто.")
        if c.message: await creator_home(c.message)
    @r.callback_query(F.data.startswith("support:creator:reopen:"))
    async def reopen(c:CallbackQuery):
        if not is_creator(c.from_user.id): return
        tid=int((c.data or "0").rsplit(":",1)[-1])
        async with session_factory() as s:
            async with s.begin():
                t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid))).scalar_one_or_none()
                if t: t.status="open"; t.closed_at=None
        await c.answer("Обращение снова открыто.")
        if c.message: await creator_home(c.message)
    @r.callback_query(F.data.startswith("support:creator:delete:"))
    async def delete(c:CallbackQuery):
        if not is_creator(c.from_user.id): return
        tid=int((c.data or "0").rsplit(":",1)[-1])
        async with session_factory() as s:
            async with s.begin():
                t=(await s.execute(select(SupportTicket).where(SupportTicket.id==tid))).scalar_one_or_none()
                if t: t.deleted_at=datetime.now(timezone.utc)
        await c.answer("Обращение удалено из рабочих списков.")
        if c.message: await creator_home(c.message)
    return r
