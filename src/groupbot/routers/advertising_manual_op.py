from __future__ import annotations
import re
from datetime import datetime,timedelta,timezone
from html import escape
from aiogram import Bot,F,Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import CallbackQuery,ChatJoinRequest,ChatMemberUpdated,InlineKeyboardButton,InlineKeyboardMarkup,Message
from sqlalchemy import and_,or_,select
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker
from groupbot.advertising_manual_models import AdvertisingManualOp,AdvertisingManualOpCredit
from groupbot.models import Group,GroupOwner,GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group

_CMD_RE=re.compile(r"(?i)^\s*подключить\s+рекламу\s+(\S+)\s+(\d+)\s+(д(?:ень|ня|ней)|участник(?:а|ов)?|подписчик(?:а|ов)?)\s*$")
_CMD_PREFIX_RE=re.compile(r"(?i)^\s*подключить\s+рекламу(?:\s+(.*?))?\s*$")
_SPEC_RE=re.compile(r"(?i)^\s*(\S+)\s+(\d+)\s+(д(?:ень|ня|ней)|участник(?:а|ов)?|подписчик(?:а|ов)?)\s*$")
class ManualOpPrivateState(StatesGroup):waiting_spec=State()
class TargetBotNotAdminError(Exception):pass
class PrivateTargetNeedsVerificationError(Exception):pass

def _number_emoji(i:int)->str:return {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}.get(i,f"{i}.")
def _public_username(v:str)->str|None:
 v=v.strip()
 if v.startswith("@"):return v[1:]
 m=re.match(r"https?://t\.me/([A-Za-z0-9_]{5,})/?$",v,re.I);return m.group(1) if m else None
def _is_telegram_target(v:str)->bool:
 return _public_username(v) is not None or bool(re.match(r"https?://t\.me/\+[A-Za-z0-9_-]+$",v,re.I)) or "t.me/joinchat/" in v.lower()
async def _source_allowed(s:AsyncSession,chat_id:int,user_id:int)->bool:
 row=(await s.execute(select(Group.chat_id).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(Group.chat_id==chat_id,Group.status==GroupStatus.active.value,GroupOwner.user_id==user_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none();return row is not None and await active_subscription_for_group(s,chat_id) is not None
async def _resolve_target(bot:Bot,v:str)->tuple[int|None,str,str]:
 u=_public_username(v)
 if u:
  c=await bot.get_chat("@"+u);return c.id,c.title or ("@"+u),f"https://t.me/{u}"
 if re.match(r"https?://t\.me/\+[A-Za-z0-9_-]+$",v,re.I) or "t.me/joinchat/" in v.lower():return None,"Закрытая рекламная группа",v
 raise ValueError
async def _ensure_bot_admin(bot:Bot,target_id:int|None)->None:
 if target_id is None:
  raise PrivateTargetNeedsVerificationError
 try:
  me=await bot.get_me()
  member=await bot.get_chat_member(target_id,me.id)
 except Exception as exc:
  raise TargetBotNotAdminError from exc
 if member.status not in {"administrator","creator"}:
  raise TargetBotNotAdminError
async def _create_op(sf,bot:Bot,*,source_chat_id:int,owner_user_id:int,target:str,quantity:int|None=None,unit:str|None=None)->AdvertisingManualOp:
 target_id,title,url=await _resolve_target(bot,target)
 await _ensure_bot_admin(bot,target_id)
 mode="unlimited" if quantity is None else ("days" if (unit or "").lower().startswith("д") else "subscribers");now=datetime.now(timezone.utc)
 async with sf() as s:
  async with s.begin():
   if not await _source_allowed(s,source_chat_id,owner_user_id):raise PermissionError
   if target_id==source_chat_id:raise ValueError
   op=AdvertisingManualOp(source_chat_id=source_chat_id,owner_user_id=owner_user_id,target_chat_id=target_id,target_url=url,target_title=title,mode=mode,quantity=quantity or 0,ends_at=now+timedelta(days=quantity) if mode=="days" else None);s.add(op);await s.flush();oid=op.id
  return (await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==oid))).scalar_one()
async def _bind_and_credit(s:AsyncSession,*,invite_url:str|None,target_chat_id:int,target_title:str,user_id:int,reason:str)->None:
 known_target=AdvertisingManualOp.target_chat_id==target_chat_id
 private_target=and_(AdvertisingManualOp.target_chat_id.is_(None),AdvertisingManualOp.target_url==invite_url) if invite_url else None
 match=or_(known_target,private_target) if private_target is not None else known_target
 ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status=="active",match).with_for_update())).scalars().all())
 for op in ops:
  if op.target_chat_id is None:
   if not invite_url or op.target_url!=invite_url:continue
   op.target_chat_id=target_chat_id;op.target_title=target_title
  if op.target_chat_id!=target_chat_id:continue
  if op.mode=="subscribers" and op.progress_count>=op.quantity:continue
  credit=(await s.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==op.id,AdvertisingManualOpCredit.user_id==user_id).with_for_update())).scalar_one_or_none()
  if credit is None:
   s.add(AdvertisingManualOpCredit(op_id=op.id,user_id=user_id,satisfied=True,counted=True,reason=reason))
   if op.mode=="subscribers":op.progress_count=min(op.progress_count+1,op.quantity)
  elif not credit.satisfied and reason=="joined":
   credit.satisfied=True;credit.reason="joined"
   if not credit.counted:
    credit.counted=True
    if op.mode=="subscribers":op.progress_count=min(op.progress_count+1,op.quantity)
  elif credit.satisfied and credit.reason=="join_request" and reason=="joined":credit.satisfied=True

def create_advertising_manual_op_router(sf:async_sessionmaker[AsyncSession])->Router:
 r=Router(name="advertising_manual_op")
 async def render(chat_id:int):
  now=datetime.now(timezone.utc)
  async with sf() as s:ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==chat_id,AdvertisingManualOp.status=="active",or_(and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity),AdvertisingManualOp.mode=="unlimited")).order_by(AdvertisingManualOp.id))).scalars().all())
  if not ops:return "📭 Активных ОП сейчас нет.",None
  lines=[f"✅ <b>Ваши активные ОП: {len(ops)}</b>",""];buttons=[]
  for i,op in enumerate(ops,1):
   lines += [f"{_number_emoji(i)} {escape(op.target_url)}",f"┣ 🆔 {op.target_chat_id if op.target_chat_id is not None else 'ожидает определения'}",f"┣ 🅰️ {escape(op.target_title)}"]
   if op.mode=="days":lines.append(f"┗ 🕐 Активна до: {op.ends_at.strftime('%d.%m.%Y %H:%M') if op.ends_at else '♾️'}")
   elif op.mode=="subscribers":lines.append(f"┗ 📍 Цель: {op.progress_count:,}/{op.quantity:,} подписчиков".replace(","," "))
   else:lines.append("┗ 🕐 Активна до: ♾️")
   lines.append("");buttons.append(InlineKeyboardButton(text=f"❌ ОТКЛ №{i}",callback_data=f"ads:manual:off:{op.id}"))
  return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0,len(buttons),2)])
 async def start_private(message:Message,state:FSMContext)->None:
  async with sf() as s:rows=(await s.execute(select(Group.chat_id,Group.title).join(GroupOwner,GroupOwner.chat_id==Group.chat_id).where(GroupOwner.user_id==message.from_user.id,GroupOwner.is_current.is_(True),Group.status==GroupStatus.active.value))).all()
  buttons=[[InlineKeyboardButton(text=(t or "Группа")[:60],callback_data=f"ads:manual:source:{cid}")] for cid,t in rows];await message.answer("Выберите группу, в которой нужно включить ОП:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_CMD_PREFIX_RE))
 async def connect(message:Message,bot:Bot):
  if message.from_user is None:return
  raw=(message.text or "").strip();full=_CMD_RE.match(raw);prefix=_CMD_PREFIX_RE.match(raw);rest=(prefix.group(1) or "").strip() if prefix else ""
  if not rest:
   await message.reply("📣 Укажите источник рекламы — Telegram-группу или канал.\n\nНапример:\n<code>подключить рекламу @group</code>\n<code>подключить рекламу https://t.me/group 100 участников</code>\n<code>подключить рекламу https://t.me/+invite 7 дней</code>",parse_mode="HTML");return
  target=rest.split()[0]
  if not _is_telegram_target(target):
   await message.reply("⚠️ На сторонние сайты и сервисы рекламу подключить нельзя. Укажите Telegram-ссылку, индивидуальную invite-ссылку или @username.");return
  if full:
   target,q,u=full.groups();q=int(q)
   if not 1<=q<=1_000_000:await message.reply("Количество должно быть от 1 до 1 000 000.");return
   try:op=await _create_op(sf,bot,source_chat_id=message.chat.id,owner_user_id=message.from_user.id,target=target,quantity=q,unit=u)
   except PrivateTargetNeedsVerificationError:await message.reply("⚠️ По приватной invite-ссылке бот не может заранее определить ID группы и проверить свои права. ОП не включена.\n\nДобавьте бота администратором в группу Б и укажите её публичный @username или ссылку https://t.me/username.");return
   except TargetBotNotAdminError:await message.reply("⛔ ОП не включена. Бот должен быть администратором в рекламной группе/канале Б, иначе он не сможет корректно проверять подписку участников.\n\nДобавьте бота администратором и повторите команду.");return
   except PermissionError:await message.reply("Подключать ОП может владелец активной группы с действующей подпиской Mimorus.");return
   except Exception:await message.reply("Не удалось определить Telegram-группу или канал. Проверьте ссылку и доступ бота.");return
   await message.reply(f"✅ ОП подключена.\n🤖 Бот проверен: администратор группы Б.\n🏠 {escape(op.target_title)}",parse_mode="HTML");return
  if len(rest.split())>1:
   await message.reply("⚠️ Не удалось распознать срок или цель. Используйте, например: <code>100 участников</code> или <code>7 дней</code>.",parse_mode="HTML");return
  try:op=await _create_op(sf,bot,source_chat_id=message.chat.id,owner_user_id=message.from_user.id,target=target)
  except PrivateTargetNeedsVerificationError:await message.reply("⚠️ По приватной invite-ссылке бот не может заранее определить ID группы и проверить свои права. ОП не включена.\n\nДобавьте бота администратором в группу Б и укажите её публичный @username или ссылку https://t.me/username.");return
  except TargetBotNotAdminError:await message.reply("⛔ ОП не включена. Бот должен быть администратором в рекламной группе/канале Б, иначе он не сможет корректно проверять подписку участников.\n\nДобавьте бота администратором и повторите команду.");return
  except PermissionError:await message.reply("Подключать ОП может владелец активной группы с действующей подпиской Mimorus.");return
  except Exception:await message.reply("Не удалось определить Telegram-группу или канал. Проверьте ссылку и доступ бота.");return
  await message.reply(f"⚠️ Период дней или количество подписчиков не указаны. Реклама будет бессрочной.\n\n✅ ОП подключена.\n🤖 Бот проверен: администратор группы Б.\n🏠 {escape(op.target_title)}",parse_mode="HTML")
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
  if c.message is not None:await c.message.edit_text("Отправьте цель и условие, например:\n<code>@channel 7 дней</code>\nили публичную Telegram-ссылку и количество участников.\n\nБот должен быть администратором в группе/канале Б.",parse_mode="HTML")
  await c.answer()
 @r.message(ManualOpPrivateState.waiting_spec,F.chat.type=="private")
 async def spec(m:Message,state:FSMContext,bot:Bot):
  if m.from_user is None:return
  x=_SPEC_RE.match(m.text or "")
  if not x:await m.answer("Формат: <code>@group 7 дней</code> или <code>ссылка 100 участников</code>",parse_mode="HTML");return
  d=await state.get_data()
  try:op=await _create_op(sf,bot,source_chat_id=int(d.get("manual_source_chat_id")),owner_user_id=m.from_user.id,target=x.group(1),quantity=int(x.group(2)),unit=x.group(3))
  except PrivateTargetNeedsVerificationError:await m.answer("⚠️ По приватной invite-ссылке нельзя заранее проверить права бота. ОП не включена. Укажите публичный @username группы Б после добавления бота администратором.");return
  except TargetBotNotAdminError:await m.answer("⛔ ОП не включена. Сначала добавьте бота администратором в группу/канал Б и повторите попытку.");return
  except Exception:await m.answer("Не удалось подключить ОП. Проверьте группу, подписку и ссылку.");return
  await state.clear();await m.answer(f"✅ ОП подключена: {escape(op.target_title)}\n🤖 Бот проверен: администратор группы Б.",parse_mode="HTML")
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