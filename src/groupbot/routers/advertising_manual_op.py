from __future__ import annotations
import re
from datetime import datetime,timedelta,timezone
from html import escape
from aiogram import Bot,F,Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import CallbackQuery,ChatJoinRequest,ChatMemberUpdated,InlineKeyboardButton,InlineKeyboardMarkup,Message
from sqlalchemy import or_,select
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker
from groupbot.advertising_manual_models import AdvertisingManualOp,AdvertisingManualOpCredit
from groupbot.models import Group,GroupOwner,GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group

_CMD_RE=re.compile(r"(?i)^\s*подключить\s+рекламу\s+(\S+)\s+(\d+)\s+(д(?:ень|ня|ней)|участник(?:а|ов)?|подписчик(?:а|ов)?)\s*$")
_SPEC_RE=re.compile(r"(?i)^\s*(\S+)\s+(\d+)\s+(д(?:ень|ня|ней)|участник(?:а|ов)?|подписчик(?:а|ов)?)\s*$")
class ManualOpPrivateState(StatesGroup):waiting_spec=State()
def _number_emoji(i:int)->str:return {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}.get(i,f"{i}.")
def _public_username(v:str)->str|None:
 v=v.strip()
 if v.startswith("@"):return v[1:]
 m=re.match(r"https?://t\.me/([A-Za-z0-9_]{5,})/?$",v,re.I);return m.group(1) if m else None
async def _source_allowed(s:AsyncSession,chat_id:int,user_id:int)->bool:
 row=(await s.execute(select(Group.chat_id).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(Group.chat_id==chat_id,Group.status==GroupStatus.active.value,GroupOwner.user_id==user_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none();return row is not None and await active_subscription_for_group(s,chat_id) is not None
async def _resolve_target(bot:Bot,v:str)->tuple[int|None,str,str]:
 u=_public_username(v)
 if u:
  c=await bot.get_chat("@"+u);return c.id,c.title or ("@"+u),f"https://t.me/{u}"
 if re.match(r"https?://t\.me/\+[A-Za-z0-9_-]+$",v,re.I) or "joinchat/" in v:return None,"Закрытая рекламная группа",v
 raise ValueError
async def _create_op(sf,bot:Bot,*,source_chat_id:int,owner_user_id:int,target:str,quantity:int,unit:str)->AdvertisingManualOp:
 target_id,title,url=await _resolve_target(bot,target);mode="days" if unit.lower().startswith("д") else "subscribers";now=datetime.now(timezone.utc)
 async with sf() as s:
  async with s.begin():
   if not await _source_allowed(s,source_chat_id,owner_user_id):raise PermissionError
   if target_id==source_chat_id:raise ValueError
   op=AdvertisingManualOp(source_chat_id=source_chat_id,owner_user_id=owner_user_id,target_chat_id=target_id,target_url=url,target_title=title,mode=mode,quantity=quantity,ends_at=now+timedelta(days=quantity) if mode=="days" else None);s.add(op);await s.flush();oid=op.id
  return (await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==oid))).scalar_one()
async def _bind_and_credit(s:AsyncSession,*,invite_url:str|None,target_chat_id:int,target_title:str,user_id:int,reason:str)->None:
 conditions=[AdvertisingManualOp.target_chat_id==target_chat_id]
 if invite_url:conditions.append(AdvertisingManualOp.target_url==invite_url)
 ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status=="active",or_(*conditions)).with_for_update())).scalars().all())
 for op in ops:
  if op.target_chat_id is None:op.target_chat_id=target_chat_id;op.target_title=target_title
  if op.target_chat_id!=target_chat_id:continue
  credit=(await s.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==op.id,AdvertisingManualOpCredit.user_id==user_id).with_for_update())).scalar_one_or_none()
  if credit is None:
   s.add(AdvertisingManualOpCredit(op_id=op.id,user_id=user_id,satisfied=True,counted=True,reason=reason))
   if op.mode=="subscribers":op.progress_count+=1
  elif not credit.satisfied and reason=="joined":
   credit.satisfied=True;credit.reason="joined"
   if not credit.counted:
    credit.counted=True
    if op.mode=="subscribers":op.progress_count+=1

def create_advertising_manual_op_router(sf:async_sessionmaker[AsyncSession])->Router:
 r=Router(name="advertising_manual_op")
 async def render(chat_id:int):
  now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():
    ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==chat_id,AdvertisingManualOp.status=="active").order_by(AdvertisingManualOp.id).with_for_update())).scalars().all());active=[]
    for op in ops:
     done=(op.mode=="days" and op.ends_at is not None and op.ends_at<=now) or (op.mode=="subscribers" and op.progress_count>=op.quantity)
     if done:op.status="completed";op.completed_at=now
     else:active.append(op)
  if not active:return "📭 Активных ОП сейчас нет.",None
  lines=[f"✅ <b>Ваши активные ОП: {len(active)}</b>",""];buttons=[]
  for i,op in enumerate(active,1):
   lines += [f"{_number_emoji(i)} {escape(op.target_url)}",f"┣ 🆔 {op.target_chat_id if op.target_chat_id is not None else 'ожидает определения'}",f"┣ 🅰️ {escape(op.target_title)}"]
   lines.append(f"┗ 🕐 Активна до: {op.ends_at.strftime('%d.%m.%Y %H:%M') if op.ends_at else '♾️'}" if op.mode=="days" else f"┗ 📍 Цель: {op.progress_count:,}/{op.quantity:,} подписчиков".replace(","," "));lines.append("");buttons.append(InlineKeyboardButton(text=f"❌ ОТКЛ №{i}",callback_data=f"ads:manual:off:{op.id}"))
  return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0,len(buttons),2)])
 async def start_private(message:Message,state:FSMContext)->None:
  async with sf() as s:rows=(await s.execute(select(Group.chat_id,Group.title).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(GroupOwner.user_id==message.from_user.id,GroupOwner.is_current.is_(True),Group.status==GroupStatus.active.value))).all()
  buttons=[[InlineKeyboardButton(text=(t or "Группа")[:60],callback_data=f"ads:manual:source:{cid}")] for cid,t in rows];await message.answer("Выберите группу, в которой нужно включить ОП:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_CMD_RE))
 async def connect(message:Message,bot:Bot):
  if message.from_user is None:return
  m=_CMD_RE.match(message.text or "");target,q,u=m.groups();q=int(q)
  if not 1<=q<=1_000_000:await message.reply("Количество должно быть от 1 до 1 000 000.");return
  try:op=await _create_op(sf,bot,source_chat_id=message.chat.id,owner_user_id=message.from_user.id,target=target,quantity=q,unit=u)
  except PermissionError:await message.reply("Подключать ОП может владелец активной группы с действующей подпиской Mimorus.");return
  except Exception:await message.reply("Не удалось определить цель. Используйте @username, публичную или индивидуальную ссылку Telegram.");return
  await message.reply(f"✅ ОП подключена.\n🏠 {escape(op.target_title)}",parse_mode="HTML")
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(r"(?i)^\s*реклама\s*$"))
 async def show(message:Message):
  if message.from_user is None:return
  async with sf() as s:
   if not await _source_allowed(s,message.chat.id,message.from_user.id):return
  text,kb=await render(message.chat.id);await message.answer(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb)
 @r.callback_query(F.data.regexp(r"^ads:manual:off:\d+$"))
 async def stop(c:CallbackQuery):
  oid=int((c.data or "").rsplit(":",1)[1]);now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():
    op=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==oid).with_for_update())).scalar_one_or_none()
    if op is None or op.status!="active" or not await _source_allowed(s,op.source_chat_id,c.from_user.id):await c.answer("ОП недоступна.",show_alert=True);return
    op.status="stopped";op.completed_at=now;cid=op.source_chat_id
  if c.message is not None:text,kb=await render(cid);await c.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb)
  await c.answer("ОП отключена")
 @r.message(F.chat.type=="private",F.text.regexp(r"(?i)^\s*(?:подключить\s+рекламу|подключить\s+оп|оп)\s*$"))
 async def private_entry(m:Message,state:FSMContext):
  if m.from_user is not None:await start_private(m,state)
 @r.callback_query(F.data=="ads:manual")
 async def private_cb(c:CallbackQuery,state:FSMContext):
  if c.message is not None:await start_private(c.message,state)
  await c.answer()
 @r.callback_query(F.data.regexp(r"^ads:manual:source:-?\d+$"))
 async def source(c:CallbackQuery,state:FSMContext):
  cid=int((c.data or "").rsplit(":",1)[1]);await state.set_state(ManualOpPrivateState.waiting_spec);await state.update_data(manual_source_chat_id=cid)
  if c.message is not None:await c.message.edit_text("Отправьте цель и условие, например:\n<code>@channel 7 дней</code>\nили индивидуальную ссылку и количество участников.",parse_mode="HTML")
  await c.answer()
 @r.message(ManualOpPrivateState.waiting_spec,F.chat.type=="private")
 async def spec(m:Message,state:FSMContext,bot:Bot):
  if m.from_user is None:return
  x=_SPEC_RE.match(m.text or "")
  if not x:await m.answer("Формат: <code>@group 7 дней</code> или <code>ссылка 100 участников</code>",parse_mode="HTML");return
  d=await state.get_data()
  try:op=await _create_op(sf,bot,source_chat_id=int(d.get("manual_source_chat_id")),owner_user_id=m.from_user.id,target=x.group(1),quantity=int(x.group(2)),unit=x.group(3))
  except Exception:await m.answer("Не удалось подключить ОП. Проверьте группу, подписку и ссылку.");return
  await state.clear();await m.answer(f"✅ ОП подключена: {escape(op.target_title)}",parse_mode="HTML")
 @r.chat_join_request()
 async def request(e:ChatJoinRequest):
  invite=e.invite_link.invite_link if e.invite_link else None
  async with sf() as s:
   async with s.begin():await _bind_and_credit(s,invite_url=invite,target_chat_id=e.chat.id,target_title=e.chat.title or "Рекламная группа",user_id=e.from_user.id,reason="join_request")
 @r.chat_member()
 async def member(e:ChatMemberUpdated):
  st=e.new_chat_member.status;joined=st in {"member","administrator","creator"} or (st=="restricted" and getattr(e.new_chat_member,"is_member",True))
  if not joined:return
  invite=e.invite_link.invite_link if e.invite_link else None
  async with sf() as s:
   async with s.begin():await _bind_and_credit(s,invite_url=invite,target_chat_id=e.chat.id,target_title=e.chat.title or "Рекламная группа",user_id=e.new_chat_member.user.id,reason="joined")
 return r
