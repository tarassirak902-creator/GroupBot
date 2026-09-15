from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualOp, AdvertisingManualOpCredit
from groupbot.models import Group, GroupOwner, GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group

_CMD_RE = re.compile(r"(?i)^\s*подключить\s+рекламу\s+(\S+)\s+(\d+)\s+(д(?:ень|ня|ней)|участник(?:а|ов)?|подписчик(?:а|ов)?)\s*$")

class ManualOpPrivateState(StatesGroup):
    waiting_spec = State()

def _number_emoji(index: int) -> str:
    return {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}.get(index, f"{index}.")

def _public_username(value: str) -> str | None:
    value=value.strip()
    if value.startswith("@"): return value[1:]
    match=re.match(r"https?://t\.me/([A-Za-z0-9_]{5,})/?$",value,re.I)
    return match.group(1) if match else None

async def _source_allowed(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    row=(await session.execute(select(Group.chat_id).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(Group.chat_id==chat_id,Group.status==GroupStatus.active.value,GroupOwner.user_id==user_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none()
    return row is not None and await active_subscription_for_group(session,chat_id) is not None

async def _resolve_target(bot: Bot,value:str)->tuple[int|None,str,str]:
    username=_public_username(value)
    if username:
        chat=await bot.get_chat("@"+username)
        return chat.id,chat.title or ("@"+username),f"https://t.me/{username}"
    if re.match(r"https?://t\.me/\+[A-Za-z0-9_-]+$",value,re.I) or "joinchat/" in value:
        return None,"Закрытая рекламная группа",value
    raise ValueError("unsupported target")

async def _create_op(session_factory,bot:Bot,*,source_chat_id:int,owner_user_id:int,target:str,quantity:int,unit:str)->AdvertisingManualOp:
    target_chat_id,title,url=await _resolve_target(bot,target); mode="days" if unit.lower().startswith("д") else "subscribers"; now=datetime.now(timezone.utc)
    async with session_factory() as session:
        async with session.begin():
            if not await _source_allowed(session,source_chat_id,owner_user_id): raise PermissionError
            if target_chat_id==source_chat_id: raise ValueError("same chat")
            op=AdvertisingManualOp(source_chat_id=source_chat_id,owner_user_id=owner_user_id,target_chat_id=target_chat_id,target_url=url,target_title=title,mode=mode,quantity=quantity,ends_at=now+timedelta(days=quantity) if mode=="days" else None)
            session.add(op); await session.flush(); op_id=op.id
        return (await session.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==op_id))).scalar_one()

async def _bind_and_credit(session:AsyncSession,*,invite_url:str|None,target_chat_id:int,target_title:str,user_id:int,reason:str)->None:
    if not invite_url:return
    ops=list((await session.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status=="active",AdvertisingManualOp.target_url==invite_url).with_for_update())).scalars().all())
    for op in ops:
        if op.target_chat_id is None: op.target_chat_id=target_chat_id; op.target_title=target_title
        if op.target_chat_id!=target_chat_id:continue
        credit=(await session.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==op.id,AdvertisingManualOpCredit.user_id==user_id).with_for_update())).scalar_one_or_none()
        if credit is None:
            session.add(AdvertisingManualOpCredit(op_id=op.id,user_id=user_id,satisfied=True,counted=True,reason=reason))
            if op.mode=="subscribers":op.progress_count+=1

def create_advertising_manual_op_router(session_factory:async_sessionmaker[AsyncSession])->Router:
    router=Router(name="advertising_manual_op")
    async def render(chat_id:int):
        async with session_factory() as session: ops=list((await session.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==chat_id,AdvertisingManualOp.status=="active").order_by(AdvertisingManualOp.id))).scalars().all())
        if not ops:return "📭 Активных ОП сейчас нет.",None
        lines=[f"✅ <b>Ваши активные ОП: {len(ops)}</b>",""]; buttons=[]
        for i,op in enumerate(ops,1):
            lines += [f"{_number_emoji(i)} {escape(op.target_url)}",f"┣ 🆔 {op.target_chat_id if op.target_chat_id is not None else 'ожидает определения'}",f"┣ 🅰️ {escape(op.target_title)}"]
            if op.mode=="days": lines.append(f"┗ 🕐 Активна до: {op.ends_at.strftime('%d.%m.%Y %H:%M') if op.ends_at else '♾️'}")
            else: lines.append(f"┗ 📍 Цель: {op.progress_count:,}/{op.quantity:,} подписчиков".replace(","," "))
            lines.append(""); buttons.append(InlineKeyboardButton(text=f"❌ ОТКЛ №{i}",callback_data=f"ads:manual:off:{op.id}"))
        return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0,len(buttons),2)])

    @router.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_CMD_RE))
    async def connect_group(message:Message,bot:Bot)->None:
        if message.from_user is None:return
        match=_CMD_RE.match(message.text or ""); target,raw_quantity,unit=match.groups(); quantity=int(raw_quantity)
        if quantity<1 or quantity>1_000_000:await message.reply("Количество должно быть от 1 до 1 000 000.");return
        try:op=await _create_op(session_factory,bot,source_chat_id=message.chat.id,owner_user_id=message.from_user.id,target=target,quantity=quantity,unit=unit)
        except PermissionError:await message.reply("Подключать ОП может владелец активной группы с действующей подпиской Mimorus.");return
        except Exception:await message.reply("Не удалось определить рекламную цель. Используйте @username, публичную или индивидуальную ссылку Telegram.");return
        await message.reply(f"✅ ОП подключена.\n🏠 {escape(op.target_title)}",parse_mode="HTML")

    @router.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(r"(?i)^\s*реклама\s*$"))
    async def show_group(message:Message)->None:
        if message.from_user is None:return
        async with session_factory() as session:
            if not await _source_allowed(session,message.chat.id,message.from_user.id):return
        text,markup=await render(message.chat.id);await message.answer(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=markup)

    @router.callback_query(F.data.regexp(r"^ads:manual:off:\d+$"))
    async def stop(callback:CallbackQuery)->None:
        op_id=int((callback.data or "").rsplit(":",1)[1]);now=datetime.now(timezone.utc)
        async with session_factory() as session:
            async with session.begin():
                op=(await session.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==op_id).with_for_update())).scalar_one_or_none()
                if op is None or op.status!="active" or not await _source_allowed(session,op.source_chat_id,callback.from_user.id):await callback.answer("ОП недоступна.",show_alert=True);return
                op.status="stopped";op.completed_at=now;source_chat_id=op.source_chat_id
        if callback.message is not None:
            text,markup=await render(source_chat_id);await callback.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=markup)
        await callback.answer("ОП отключена")

    @router.callback_query(F.data=="ads:manual")
    async def private_start(callback:CallbackQuery,state:FSMContext)->None:
        async with session_factory() as session:rows=(await session.execute(select(Group.chat_id,Group.title).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(GroupOwner.user_id==callback.from_user.id,GroupOwner.is_current.is_(True),Group.status==GroupStatus.active.value))).all()
        buttons=[[InlineKeyboardButton(text=(title or "Группа")[:60],callback_data=f"ads:manual:source:{chat_id}")] for chat_id,title in rows]
        if callback.message is not None:await callback.message.edit_text("Выберите группу, в которой нужно включить ОП:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="◀️ Назад",callback_data="ads:home")]]))
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^ads:manual:source:-?\d+$"))
    async def private_source(callback:CallbackQuery,state:FSMContext)->None:
        chat_id=int((callback.data or "").rsplit(":",1)[1]);await state.set_state(ManualOpPrivateState.waiting_spec);await state.update_data(manual_source_chat_id=chat_id)
        if callback.message is not None:await callback.message.edit_text("Отправьте цель и условие, например:\n<code>@channel 7 дней</code>\nили индивидуальную ссылку и количество участников.",parse_mode="HTML")
        await callback.answer()

    @router.message(ManualOpPrivateState.waiting_spec,F.chat.type=="private")
    async def private_spec(message:Message,state:FSMContext,bot:Bot)->None:
        if message.from_user is None:return
        match=re.match(r"(?i)^\s*(\S+)\s+(\d+)\s+(д(?:ень|ня|ней)|участник(?:а|ов)?|подписчик(?:а|ов)?)\s*$",message.text or "")
        if not match:await message.answer("Формат: <code>@group 7 дней</code> или <code>ссылка 100 участников</code>",parse_mode="HTML");return
        data=await state.get_data()
        try:op=await _create_op(session_factory,bot,source_chat_id=int(data.get("manual_source_chat_id")),owner_user_id=message.from_user.id,target=match.group(1),quantity=int(match.group(2)),unit=match.group(3))
        except Exception:await message.answer("Не удалось подключить ОП. Проверьте группу, подписку и ссылку.");return
        await state.clear();await message.answer(f"✅ ОП подключена: {escape(op.target_title)}",parse_mode="HTML")

    @router.chat_join_request()
    async def join_request(event:ChatJoinRequest)->None:
        invite=event.invite_link.invite_link if event.invite_link else None
        async with session_factory() as session:
            async with session.begin():await _bind_and_credit(session,invite_url=invite,target_chat_id=event.chat.id,target_title=event.chat.title or "Рекламная группа",user_id=event.from_user.id,reason="join_request")

    @router.chat_member()
    async def member_update(event:ChatMemberUpdated)->None:
        status=event.new_chat_member.status;member=status in {"member","administrator","creator"} or (status=="restricted" and getattr(event.new_chat_member,"is_member",True))
        if not member:return
        invite=event.invite_link.invite_link if event.invite_link else None
        async with session_factory() as session:
            async with session.begin():await _bind_and_credit(session,invite_url=invite,target_chat_id=event.chat.id,target_title=event.chat.title or "Рекламная группа",user_id=event.new_chat_member.user.id,reason="joined")
    return router
