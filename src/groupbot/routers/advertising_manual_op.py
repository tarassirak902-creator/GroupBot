from __future__ import annotations
import re
from datetime import datetime,timedelta,timezone
from html import escape
from aiogram import Bot,F,Router
from aiogram.types import CallbackQuery,InlineKeyboardButton,InlineKeyboardMarkup,Message
from sqlalchemy import and_,or_,select
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker
from groupbot.advertising_manual_models import AdvertisingManualLink,AdvertisingManualOp
from groupbot.models import Group,GroupOwner,GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group

_CMD_RE=re.compile(r"(?i)^\s*подключить\s+рекламу\s+(\S+)(?:\s+(\d+)\s+(д(?:ень|ня|ней)|уч(?:астник(?:а|ов)?)?|подписчик(?:а|ов)?))?\s*$")
_PREFIX_RE=re.compile(r"(?i)^\s*подключить\s+рекламу(?:\s+(.*?))?\s*$")
_LINK_RE=re.compile(r"(?i)^\s*/ссылка(?:@\w+)?(?:\s+(\d+)\s+(д(?:ень|ня|ней)|уч(?:астник(?:а|ов)?)?|подписчик(?:а|ов)?))?\s*$")
def _mode(q,u):return "unlimited" if q is None else ("days" if (u or "").lower().startswith("д") else "subscribers")
def _condition(mode,q):return "бессрочно" if mode=="unlimited" else (f"{q} дней" if mode=="days" else f"{q:,} участников".replace(","," "))
def _is_tg(v):return bool(re.match(r"(?i)^https?://t\.me/(?:\+[A-Za-z0-9_-]+|[A-Za-z0-9_]{5,})/?$",v)) or v.startswith("@")
async def _owner(s,chat_id,user_id):return (await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id==chat_id,GroupOwner.user_id==user_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none() is not None
async def _source_allowed(s,chat_id,user_id):
 return await _owner(s,chat_id,user_id) and (await s.execute(select(Group.status).where(Group.chat_id==chat_id))).scalar_one_or_none()==GroupStatus.active.value and await active_subscription_for_group(s,chat_id) is not None
async def _bot_admin(bot,chat_id):
 try:m=await bot.get_chat_member(chat_id,(await bot.get_me()).id);return m.status in {"administrator","creator"}
 except Exception:return False

def create_advertising_manual_op_router(sf:async_sessionmaker[AsyncSession])->Router:
 r=Router(name="advertising_manual_op")
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_LINK_RE))
 async def make_link(m:Message,bot:Bot):
  if not m.from_user:return
  x=_LINK_RE.match(m.text or "");q=int(x.group(1)) if x and x.group(1) else None;u=x.group(2) if x else None
  if q is not None and not 1<=q<=1_000_000:await m.reply("Количество должно быть от 1 до 1 000 000.");return
  async with sf() as s:
   if not await _owner(s,m.chat.id,m.from_user.id):await m.reply("Создать рекламную ссылку может только владелец этой группы.");return
  if not await _bot_admin(bot,m.chat.id):await m.reply("⛔ Mimorus должен быть администратором этой группы.");return
  try:inv=await bot.create_chat_invite_link(m.chat.id,name="Mimorus advertising")
  except Exception:await m.reply("⛔ Не удалось создать ссылку. Дайте Mimorus право приглашать пользователей.");return
  mode=_mode(q,u);title=m.chat.title or str(m.chat.id)
  async with sf() as s:
   async with s.begin():s.add(AdvertisingManualLink(target_chat_id=m.chat.id,owner_user_id=m.from_user.id,invite_url=inv.invite_link,target_title=title,mode=mode,quantity=q or 0))
  suffix="" if mode=="unlimited" else (f" {q} дней" if mode=="days" else f" {q} уч")
  await m.reply(f"🔗 <b>Рекламная ссылка создана</b>\n\n<code>Подключить рекламу {escape(inv.invite_link)}{suffix}</code>\n\n{'♾️' if mode=='unlimited' else ('📅' if mode=='days' else '👥')} Условие: <b>{_condition(mode,q or 0)}</b>\n\nПередайте этот текст владельцу группы, где хотите включить ОП.",parse_mode="HTML",disable_web_page_preview=True)
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(_PREFIX_RE))
 async def connect(m:Message,bot:Bot):
  if not m.from_user:return
  raw=(m.text or "").strip();x=_CMD_RE.match(raw);p=_PREFIX_RE.match(raw);rest=(p.group(1) or "").strip() if p else ""
  if not rest:await m.reply("📣 Укажите рекламную Telegram-ссылку.");return
  if not x:
   target=rest.split()[0] if rest else ""
   if not _is_tg(target):await m.reply("⚠️ На сторонние сайты рекламу поставить нельзя. Разрешены только Telegram-ссылки.")
   else:await m.reply("⚠️ Не удалось распознать условие. Например: <code>1 день</code>, <code>25 дней</code> или <code>200 уч</code>.",parse_mode="HTML")
   return
  target=x.group(1);q=int(x.group(2)) if x.group(2) else None;u=x.group(3);requested_mode=_mode(q,u)
  if not _is_tg(target):await m.reply("⚠️ На сторонние сайты рекламу поставить нельзя. Разрешены только Telegram-ссылки.");return
  async with sf() as s:
   link=(await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.invite_url==target))).scalar_one_or_none()
   if link:
    if requested_mode!=link.mode or (q or 0)!=link.quantity:await m.reply("⚠️ Условие изменено. Используйте текст рекламной ссылки без изменений.");return
    target_id,title=link.target_chat_id,link.target_title
   else:
    if target.startswith("https://t.me/+"):await m.reply("⚠️ Эта индивидуальная ссылка не зарегистрирована в Mimorus. Создайте её в рекламной группе командой <code>/ссылка</code>.",parse_mode="HTML");return
    try:c=await bot.get_chat(target if target.startswith("@") else "@"+target.rstrip("/").rsplit("/",1)[-1]);target_id,title=c.id,c.title or target
    except Exception:await m.reply("Не удалось определить Telegram-группу или канал.");return
   if not await _source_allowed(s,m.chat.id,m.from_user.id):await m.reply("Подключать ОП может владелец активной группы с действующей подпиской Mimorus.");return
  if target_id==m.chat.id:await m.reply("Нельзя подключить рекламу группы на саму себя.");return
  if not await _bot_admin(bot,target_id):await m.reply("⛔ ОП не включена: Mimorus должен быть администратором рекламной группы Б.");return
  mode=requested_mode;now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():
    op=AdvertisingManualOp(source_chat_id=m.chat.id,owner_user_id=m.from_user.id,target_chat_id=target_id,target_url=target,target_title=title,mode=mode,quantity=q or 0,ends_at=now+timedelta(days=q) if mode=="days" and q else None);s.add(op)
  warning="\n⚠️ Реклама бессрочная: срок или количество участников не указаны." if mode=="unlimited" else ""
  await m.reply(f"✅ <b>ОП подключена</b>\n🏠 {escape(title)}\n📍 Условие: {_condition(mode,q or 0)}{warning}",parse_mode="HTML",disable_web_page_preview=True)
 async def render(chat_id):
  now=datetime.now(timezone.utc)
  async with sf() as s:ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==chat_id,AdvertisingManualOp.status=="active",or_(and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity),AdvertisingManualOp.mode=="unlimited")).order_by(AdvertisingManualOp.id))).scalars().all())
  if not ops:return "📭 Активных ОП сейчас нет.",None
  lines=[f"✅ <b>Ваши активные ОП: {len(ops)}</b>",""];buttons=[]
  for i,op in enumerate(ops,1):
   lines += [f"{i}️⃣ {escape(op.target_url)}",f"┣ 🆔 {op.target_chat_id}",f"┣ 🅰️ {escape(op.target_title)}",(f"┗ 📍 Цель: {op.progress_count:,}/{op.quantity:,} подписчиков".replace(","," ") if op.mode=="subscribers" else f"┗ 🕐 Активна до: {op.ends_at.strftime('%d.%m.%Y %H:%M') if op.ends_at else '♾️'}"),""];buttons.append(InlineKeyboardButton(text=f"❌ ОТКЛ №{i}",callback_data=f"ads:manual:off:{op.id}"))
  return "\n".join(lines).rstrip(),InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0,len(buttons),2)])
 @r.message(F.chat.type.in_({"group","supergroup"}),F.text.regexp(r"(?i)^\s*реклама\s*$"))
 async def show(m:Message):
  if not m.from_user:return
  async with sf() as s:
   if not await _source_allowed(s,m.chat.id,m.from_user.id):return
  text,kb=await render(m.chat.id);await m.answer(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb)
 @r.callback_query(F.data.regexp(r"^ads:manual:off:\d+$"))
 async def stop(c:CallbackQuery):
  oid=int((c.data or "").rsplit(":",1)[1]);now=datetime.now(timezone.utc)
  async with sf() as s:
   async with s.begin():
    op=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==oid).with_for_update())).scalar_one_or_none()
    if op is None or op.status!="active" or not await _source_allowed(s,op.source_chat_id,c.from_user.id):await c.answer("ОП недоступна.",show_alert=True);return
    op.status="stopped";op.completed_at=now;cid=op.source_chat_id;target_id=op.target_chat_id;url=op.target_url
  try:await c.bot.revoke_chat_invite_link(target_id,url)
  except Exception:pass
  if c.message:text,kb=await render(cid);await c.message.edit_text(text,parse_mode="HTML",disable_web_page_preview=True,reply_markup=kb)
  await c.answer("ОП отключена")
 return r
