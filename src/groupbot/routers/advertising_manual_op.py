from __future__ import annotations
import re
from datetime import datetime,timedelta,timezone
from html import escape
from aiogram import Bot,F,Router
from aiogram.types import CallbackQuery,InlineKeyboardButton,InlineKeyboardMarkup,Message
from sqlalchemy import and_,or_,select
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker
from groupbot.advertising_manual_models import AdvertisingManualLink,AdvertisingManualOp,AdvertisingManualOpCredit
from groupbot.models import AdminAssignment,AdminPermission,AdminRole,Group,GroupOwner,GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group
from groupbot.routers import group_control as _group_control

ADVERTISING_PERMISSION="advertising_manage"
if ADVERTISING_PERMISSION not in {key for key,_ in _group_control.KNOWN_PERMISSIONS}:
 _group_control.KNOWN_PERMISSIONS.append((ADVERTISING_PERMISSION,"📢 Управление рекламой"))

_CMD_RE=re.compile(r"(?i)^\s*подключить\s+рекламу\s+(\S+)(?:\s+(\d+)\s+(д(?:ень|ня|ней)|уч(?:астник(?:а|ов)?)?|подписчик(?:а|ов)?))?\s*$");_PREFIX_RE=re.compile(r"(?i)^\s*подключить\s+рекламу(?:\s+(.*?))?\s*$");_LINK_RE=re.compile(r"(?i)^\s*/ссылка(?:@\w+)?(?:\s+(\d+)\s+(д(?:ень|ня|ней)|уч(?:астник(?:а|ов)?)?|подписчик(?:а|ов)?))?\s*$")
def _mode(q,u):return "unlimited" if q is None else ("days" if (u or "").lower().startswith("д") else "subscribers")
def _condition(mode,q):return "бессрочно" if mode=="unlimited" else (f"{q} дней" if mode=="days" else f"{q:,} участников".replace(","," "))
def _is_tg(v):return bool(re.match(r"(?i)^https?://t\.me/(?:\+[A-Za-z0-9_-]+|[A-Za-z0-9_]{5,})/?$",v)) or v.startswith("@")
def _invite_name(source_title:str|None=None)->str:
 base="Mimorus OP"
 if not source_title:return base
 title=" ".join(source_title.split())
 return f"{base} • {title}"[:32]
async def _owner(s,chat_id,user_id):return (await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id==chat_id,GroupOwner.user_id==user_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none() is not None
async def _advertising_access(s:AsyncSession,chat_id:int,user_id:int)->bool:
 if await _owner(s,chat_id,user_id):return True
 return (await s.execute(select(AdminAssignment.id).join(AdminRole,AdminRole.id==AdminAssignment.role_id).join(AdminPermission,and_(AdminPermission.role_id==AdminRole.id,AdminPermission.permission==ADVERTISING_PERMISSION,AdminPermission.allowed.is_(True))).where(AdminAssignment.chat_id==chat_id,AdminAssignment.user_id==user_id,AdminRole.name=="Зам. владельца",AdminRole.is_active.is_(True)).limit(1))).scalar_one_or_none() is not None
async def _source_allowed(s,chat_id,user_id):return await _advertising_access(s,chat_id,user_id) and (await s.execute(select(Group.status).where(Group.chat_id==chat_id))).scalar_one_or_none()==GroupStatus.active.value and await active_subscription_for_group(s,chat_id) is not None
async def _current_owner_id(s,chat_id):return (await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id==chat_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none()
async def _bot_admin(bot,chat_id):
 try:m=await bot.get_chat_member(chat_id,(await bot.get_me()).id);return m.status in {"administrator","creator"}
 except Exception:return False
async def _bind_and_credit(s:AsyncSession,*,invite_url:str|None,target_chat_id:int,target_title:str,user_id:int,reason:str)->None:
 if not invite_url:return
 ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.target_chat_id==target_chat_id,AdvertisingManualOp.target_url==invite_url,AdvertisingManualOp.status=="active",or_(AdvertisingManualOp.mode=="unlimited",AdvertisingManualOp.mode=="days",and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity))).with_for_update())).scalars().all())
 for op in ops:
  credit=(await s.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==op.id,AdvertisingManualOpCredit.user_id==user_id).with_for_update())).scalar_one_or_none()
  if credit is None:
   counted=op.mode=="subscribers";s.add(AdvertisingManualOpCredit(op_id=op.id,user_id=user_id,satisfied=True,counted=counted,reason=reason))
   if counted:op.progress_count=min(op.progress_count+1,op.quantity)
  else:
   credit.satisfied=True;credit.reason=reason
   if op.mode=="subscribers" and not credit.counted and op.progress_count<op.quantity:credit.counted=True;op.progress_count+=1

def create_advertising_manual_op_router(sf:async_sessionmaker[AsyncSession])->Router:
 r=Router(name="advertising_manual_op")
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_LINK_RE))
 async def make_link(m:Message,bot:Bot):
  if not m.from_user:return
  async with sf() as s:
   if not await _advertising_access(s,m.chat.id,m.from_user.id):return
   if (await s.execute(select(Group.status).where(Group.chat_id==m.chat.id))).scalar_one_or_none()!=GroupStatus.active.value:await m.reply("⛔ Эта группа сейчас недоступна для рекламы.");return
   owner_id=await _current_owner_id(s,m.chat.id)
  x=_LINK_RE.match(m.text or "");q=int(x.group(1)) if x and x.group(1) else None;u=x.group(2) if x else None
  if q is not None and not 1<=q<=1_000_000:await m.reply("Количество должно быть от 1 до 1 000 000.");return
  if not await _bot_admin(bot,m.chat.id):await m.reply("⛔ Mimorus должен быть администратором этой группы.");return
  try:inv=await bot.create_chat_invite_link(m.chat.id,name=_invite_name(),expire_date=None,member_limit=None)
  except Exception:await m.reply("⛔ Не удалось создать ссылку. Дайте Mimorus право создавать пригласительные ссылки.");return
  mode=_mode(q,u);title=m.chat.title or str(m.chat.id)
  async with sf() as s:
   async with s.begin():s.add(AdvertisingManualLink(target_chat_id=m.chat.id,owner_user_id=owner_id or m.from_user.id,invite_url=inv.invite_link,target_title=title,mode=mode,quantity=q or 0))
  suffix="" if mode=="unlimited" else (f" {q} дней" if mode=="days" else f" {q} уч")
  await m.reply(f"🔗 <b>Рекламная ссылка создана</b>\n\n<code>Подключить рекламу {escape(inv.invite_link)}{suffix}</code>\n\n{'♾️' if mode=='unlimited' else ('📅' if mode=='days' else '👥')} Условие: <b>{_condition(mode,q or 0)}</b>\n\nПередайте этот текст владельцу группы, где хотите включить ОП.",parse_mode="HTML",disable_web_page_preview=True)

 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_PREFIX_RE))
 async def connect(m:Message,bot:Bot):
  if not m.from_user:return
  async with sf() as s:
   if not await _source_allowed(s,m.chat.id,m.from_user.id):return
   owner_id=await _current_owner_id(s,m.chat.id)
  raw=(m.text or "").strip();x=_CMD_RE.match(raw);p=_PREFIX_RE.match(raw);rest=(p.group(1) or "").strip() if p else ""
  if not rest:await m.reply("📣 Укажите рекламную Telegram-ссылку.");return
  if not x:
   target=rest.split()[0] if rest else ""
   if not _is_tg(target):await m.reply("⚠️ На сторонние сайты рекламу поставить нельзя. Разрешены только Telegram-ссылки.")
   else:await m.reply("⚠️ Не удалось распознать условие. Например: <code>1 день</code>, <code>25 дней</code> или <code>200 уч</code>.",parse_mode="HTML")
   return
  target=x.group(1);q=int(x.group(2)) if x.group(2) else None;u=x.group(3);requested_mode=_mode(q,u)
  if q is not None and not 1<=q<=1_000_000:await m.reply("Количество должно быть от 1 до 1 000 000.");return
  if not _is_tg(target):await m.reply("⚠️ На сторонние сайты рекламу поставить нельзя. Разрешены только Telegram-ссылки.");return
  async with sf() as s:
   link=(await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.invite_url==target))).scalar_one_or_none()
   if link:
    if requested_mode!=link.mode or (q or 0)!=link.quantity:await m.reply("⚠️ Условие изменено. Используйте текст рекламной ссылки без изменений.");return
    current_target_owner=await _current_owner_id(s,link.target_chat_id);target_status=(await s.execute(select(Group.status).where(Group.chat_id==link.target_chat_id))).scalar_one_or_none()
    if current_target_owner!=link.owner_user_id or target_status!=GroupStatus.active.value:await m.reply("⛔ Эта рекламная ссылка больше недействительна: группа Б недоступна или у неё сменился владелец.");return
    target_id,title=link.target_chat_id,link.target_title
   else:
    if target.startswith("https://t.me/+"):await m.reply("⚠️ Эта индивидуальная ссылка не зарегистрирована в Mimorus. Создайте её в рекламной группе командой <code>/ссылка</code>.",parse_mode="HTML");return
    try:c=await bot.get_chat(target if target.startswith("@") else "@"+target.rstrip("/").rsplit("/",1)[-1]);target_id,title=c.id,c.title or target
    except Exception:await m.reply("Не удалось определить Telegram-группу или канал.");return
  if target_id==m.chat.id:await m.reply("Нельзя подключить рекламу группы на саму себя.");return
  if not await _bot_admin(bot,target_id):await m.reply("⛔ ОП не включена: Mimorus должен быть администратором рекламной группы Б.");return
  if link:
   try:await bot.edit_chat_invite_link(target_id,target,name=_invite_name(m.chat.title or str(m.chat.id)),expire_date=None,member_limit=None)
   except Exception:await m.reply("⛔ ОП не включена: Mimorus не смог подготовить рекламную ссылку. Проверьте право бота управлять пригласительными ссылками.");return
  now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():s.add(AdvertisingManualOp(source_chat_id=m.chat.id,owner_user_id=owner_id or m.from_user.id,target_chat_id=target_id,target_url=target,target_title=title,mode=requested_mode,quantity=q or 0,ends_at=now+timedelta(days=q) if requested_mode=="days" and q else None))
  warning="\n⚠️ Реклама бессрочная: срок или количество участников не указаны." if requested_mode=="unlimited" else ""
  await m.reply(f"✅ <b>ОП подключена</b>\n🏠 {escape(title)}\n🔗 Ссылка: <code>{escape(target)}</code>\n📍 Условие: {_condition(requested_mode,q or 0)}{warning}",parse_mode="HTML",disable_web_page_preview=True)

 def menu_kb():
  return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Мы рекламируем",callback_data="ads:menu:out")],[InlineKeyboardButton(text="📥 Нас рекламируют",callback_data="ads:menu:in")],[InlineKeyboardButton(text="🔗 Мои ссылки",callback_data="ads:menu:links")]])
 async def menu_text(chat_id:int)->str:
  now=datetime.now(timezone.utc)
  async with sf() as s:
   outgoing=len(list((await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.source_chat_id==chat_id,AdvertisingManualOp.status=="active",or_(AdvertisingManualOp.mode=="unlimited",and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity))))).scalars().all()))
   incoming=len(list((await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_chat_id==chat_id,AdvertisingManualOp.status=="active",or_(AdvertisingManualOp.mode=="unlimited",and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity))))).scalars().all()))
   links=len(list((await s.execute(select(AdvertisingManualLink.id).where(AdvertisingManualLink.target_chat_id==chat_id))).scalars().all()))
  return f"📢 <b>Реклама группы</b>\n\n📤 Мы рекламируем: <b>{outgoing}</b>\n📥 Нас рекламируют: <b>{incoming}</b>\n🔗 Создано ссылок: <b>{links}</b>\n\nВыберите раздел:"
 async def render_out(chat_id:int):
  now=datetime.now(timezone.utc)
  async with sf() as s:ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==chat_id,AdvertisingManualOp.status=="active",or_(AdvertisingManualOp.mode=="unlimited",and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity))).order_by(AdvertisingManualOp.id))).scalars().all())
  lines=["📤 <b>Мы рекламируем</b>",""];rows=[]
  if not ops:lines.append("📭 Активных ОП сейчас нет.")
  for i,op in enumerate(ops,1):
   state=(f"{op.progress_count:,}/{op.quantity:,} подписчиков".replace(","," ") if op.mode=="subscribers" else (f"до {op.ends_at.strftime('%d.%m.%Y %H:%M')}" if op.mode=="days" and op.ends_at else "бессрочно"));lines += [f"{i}️⃣ <b>{escape(op.target_title)}</b>",f"┣ 🆔 {op.target_chat_id}",f"┣ 📍 {state}",f"┗ 🔗 <code>{escape(op.target_url)}</code>",""];rows.append([InlineKeyboardButton(text=f"⛔ Отключить №{i}",callback_data=f"ads:manual:off:{op.id}")])
  rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="ads:menu:back")]);return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=rows)
 async def render_in(chat_id:int):
  now=datetime.now(timezone.utc)
  async with sf() as s:ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.target_chat_id==chat_id,AdvertisingManualOp.status=="active",or_(AdvertisingManualOp.mode=="unlimited",and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity))).order_by(AdvertisingManualOp.id))).scalars().all())
  lines=["📥 <b>Нас рекламируют</b>",""];rows=[]
  if not ops:lines.append("📭 Сейчас вашу группу никто не рекламирует.")
  for i,op in enumerate(ops,1):
   state=(f"{op.progress_count:,}/{op.quantity:,} подписчиков".replace(","," ") if op.mode=="subscribers" else (f"до {op.ends_at.strftime('%d.%m.%Y %H:%M')}" if op.mode=="days" and op.ends_at else "бессрочно"));lines += [f"{i}️⃣ <b>ОП из группы {op.source_chat_id}</b>",f"┣ 📍 {state}",f"┗ 🔗 <code>{escape(op.target_url)}</code>",""];rows.append([InlineKeyboardButton(text=f"⛔ Завершить №{i}",callback_data=f"ads:target:off:{op.id}")])
  rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="ads:menu:back")]);return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=rows)
 async def render_links(chat_id:int):
  async with sf() as s:
   links=list((await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.target_chat_id==chat_id).order_by(AdvertisingManualLink.id.desc()).limit(30))).scalars().all());active_urls=set((await s.execute(select(AdvertisingManualOp.target_url).where(AdvertisingManualOp.target_chat_id==chat_id,AdvertisingManualOp.status=="active"))).scalars().all())
  lines=["🔗 <b>Мои рекламные ссылки</b>",""];rows=[]
  if not links:lines.append("📭 Рекламных ссылок ещё нет.\nСоздайте первую командой <code>/ссылка</code>.")
  for i,link in enumerate(links,1):
   used=link.invite_url in active_urls;lines += [f"{i}️⃣ <code>{escape(link.invite_url)}</code>",f"┣ 🎯 {_condition(link.mode,link.quantity)}",f"┗ {'🟢 Используется в активной ОП' if used else '⚪ Сейчас не используется'}",""]
   if not used:rows.append(InlineKeyboardButton(text=f"🗑 Удалить №{i}",callback_data=f"ads:link:delete:{link.id}"))
  button_rows=[rows[i:i+2] for i in range(0,len(rows),2)];button_rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="ads:menu:back")]);return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=button_rows)

 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(r"(?i)^\s*реклама\s*$"))
 async def show(m:Message):
  if not m.from_user:return
  async with sf() as s:
   if not await _advertising_access(s,m.chat.id,m.from_user.id):return
  await m.answer(await menu_text(m.chat.id),parse_mode="HTML",reply_markup=menu_kb())

 @r.callback_query(F.data.in_({"ads:menu:out","ads:menu:in","ads:menu:links","ads:menu:back"}))
 async def navigate(c:CallbackQuery):
  if not c.message:return
  chat_id=c.message.chat.id
  async with sf() as s:
   if not await _advertising_access(s,chat_id,c.from_user.id):await c.answer();return
  if c.data=="ads:menu:back":text,kb=await menu_text(chat_id),menu_kb()
  elif c.data=="ads:menu:out":text,kb=await render_out(chat_id)
  elif c.data=="ads:menu:in":text,kb=await render_in(chat_id)
  else:text,kb=await render_links(chat_id)
  await c.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb);await c.answer()

 @r.callback_query(F.data.regexp(r"^ads:link:delete:\d+$"))
 async def delete_link(c:CallbackQuery):
  if not c.message:return
  link_id=int((c.data or "").rsplit(":",1)[1]);chat_id=c.message.chat.id
  async with sf() as s:
   async with s.begin():
    if not await _advertising_access(s,chat_id,c.from_user.id):await c.answer();return
    link=(await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.id==link_id).with_for_update())).scalar_one_or_none()
    if link is None or link.target_chat_id!=chat_id:await c.answer();return
    active=(await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_chat_id==chat_id,AdvertisingManualOp.target_url==link.invite_url,AdvertisingManualOp.status=="active").limit(1))).scalar_one_or_none()
    if active is not None:await c.answer("Эта ссылка используется в активной ОП и не может быть удалена.",show_alert=True);return
    url=link.invite_url;await s.delete(link)
  try:await c.bot.revoke_chat_invite_link(chat_id,url)
  except Exception:pass
  text,kb=await render_links(chat_id);await c.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb);await c.answer("Ссылка удалена")

 @r.callback_query(F.data.regexp(r"^ads:manual:off:\d+$"))
 async def stop(c:CallbackQuery):
  oid=int((c.data or "").rsplit(":",1)[1]);now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():
    op=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==oid).with_for_update())).scalar_one_or_none()
    if op is None or op.status!="active" or not await _source_allowed(s,op.source_chat_id,c.from_user.id):await c.answer();return
    op.status="stopped";op.completed_at=now;cid=op.source_chat_id;target_id=op.target_chat_id;url=op.target_url
  try:await c.bot.revoke_chat_invite_link(target_id,url)
  except Exception:pass
  if c.message:text,kb=await render_out(cid);await c.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb)
  await c.answer("ОП отключена")

 @r.callback_query(F.data.regexp(r"^ads:target:off:\d+$"))
 async def target_stop(c:CallbackQuery):
  oid=int((c.data or "").rsplit(":",1)[1]);now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():
    op=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==oid).with_for_update())).scalar_one_or_none()
    if op is None or op.status!="active" or not await _advertising_access(s,op.target_chat_id,c.from_user.id):await c.answer();return
    op.status="stopped";op.completed_at=now;target_id=op.target_chat_id;url=op.target_url
  try:await c.bot.revoke_chat_invite_link(target_id,url)
  except Exception:pass
  if c.message:text,kb=await render_in(target_id);await c.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb)
  await c.answer("ОП завершена")
 return r
