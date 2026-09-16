from __future__ import annotations
import asyncio,logging
from datetime import datetime,timezone
from html import escape
from aiogram import Bot
from sqlalchemy import delete,select
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker
from groupbot.advertising_manual_models import AdvertisingManualLink,AdvertisingManualOp
from groupbot.models import Group,GroupOwner,GroupStatus
from groupbot.services.subscriptions import active_subscription_for_group
logger=logging.getLogger(__name__)

def _group_link(chat_id:int,title:str)->str:
 return f'<a href="https://t.me/MimorusBot?startgroup=manage_{abs(chat_id)}">{escape(title)}</a>'

def _completion_text(op,source_title,target_title,recipient_kind):
 source=_group_link(op.source_chat_id,source_title);target=_group_link(op.target_chat_id,target_title) if op.target_chat_id is not None else escape(target_title)
 if op.mode=="subscribers":
  result=f"🎯 Цель достигнута: <b>{op.progress_count:,}/{op.quantity:,} подписчиков</b>".replace(","," ")
 elif op.mode=="days":
  result=f"⏱ Срок рекламы завершён: <b>{op.quantity} дней</b>"
 else:
  result="✅ Реклама завершена."
 if recipient_kind=="source":
  body=f"📢 Реклама группы {target} завершена.\n{result}\n\n🔗 Рекламная ссылка больше не используется."
 else:
  body=f"📢 Группа {source} завершила рекламу вашей группы {target}.\n{result}\n\n🔗 Индивидуальная рекламная ссылка удалена."
 return f"✅ <b>Реклама завершена</b>\n\n{body}"

async def _notify(bot,op,source_title,target_title,source_owner_id,target_owner_id):
 recipients=[]
 if source_owner_id is not None:recipients.append((source_owner_id,"source"))
 if target_owner_id is not None:recipients.append((target_owner_id,"target"))
 sent=set()
 for uid,kind in recipients:
  if uid in sent:continue
  sent.add(uid)
  try:await bot.send_message(uid,_completion_text(op,source_title,target_title,kind),parse_mode="HTML",disable_web_page_preview=True)
  except Exception:logger.exception("Could not notify manual advertising completion op=%s user=%s",op.id,uid)
async def _revoke(bot,op):
 if op.target_chat_id is None:return
 try:await bot.revoke_chat_invite_link(op.target_chat_id,op.target_url)
 except Exception:pass
async def _delete_registered_link(s:AsyncSession,op:AdvertisingManualOp)->None:
 await s.execute(delete(AdvertisingManualLink).where(AdvertisingManualLink.invite_url==op.target_url))
async def _cleanup_finished_links(s:AsyncSession)->int:
 links=list((await s.execute(select(AdvertisingManualLink))).scalars().all());removed=0
 for link in links:
  active=(await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_url==link.invite_url,AdvertisingManualOp.status=="active").limit(1))).scalar_one_or_none()
  if active is not None:continue
  finished=(await s.execute(select(AdvertisingManualOp.id).where(AdvertisingManualOp.target_url==link.invite_url,AdvertisingManualOp.status.in_(("completed","stopped"))).limit(1))).scalar_one_or_none()
  if finished is not None:await s.delete(link);removed+=1
 return removed
async def _bot_admin(bot,chat_id):
 try:m=await bot.get_chat_member(chat_id,(await bot.get_me()).id);return m.status in {"administrator","creator"}
 except Exception:return False
async def run_advertising_manual_lifecycle_once(bot:Bot,session_factory:async_sessionmaker[AsyncSession])->int:
 now=datetime.now(timezone.utc);changed=0;notifications=[];revoke=[]
 async with session_factory() as s:
  async with s.begin():
   ops=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status=="active").order_by(AdvertisingManualOp.id).with_for_update(skip_locked=True))).scalars().all())
   for op in ops:
    completed=(op.mode=="days" and op.ends_at is not None and op.ends_at<=now) or (op.mode=="subscribers" and op.progress_count>=op.quantity)
    source_status=(await s.execute(select(Group.status).where(Group.chat_id==op.source_chat_id))).scalar_one_or_none();source_owner=(await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id==op.source_chat_id,GroupOwner.is_current.is_(True)))).scalar_one_or_none();source_ok=source_status==GroupStatus.active.value and source_owner==op.owner_user_id and await active_subscription_for_group(s,op.source_chat_id) is not None
    target_owner=None;target_ok=False
    if op.target_chat_id is not None:
     target_status=(await s.execute(select(Group.status).where(Group.chat_id==op.target_chat_id))).scalar_one_or_none();target_owner=(await s.execute(select(GroupOwner.user_id).where(GroupOwner.chat_id==op.target_chat_id,GroupOwner.is_current.is_(True)))).scalar_one_or_none();link=(await s.execute(select(AdvertisingManualLink).where(AdvertisingManualLink.invite_url==op.target_url))).scalar_one_or_none();target_ok=target_status==GroupStatus.active.value and target_owner is not None and (link is None or link.owner_user_id==target_owner)
    if completed:
     source_title=(await s.execute(select(Group.title).where(Group.chat_id==op.source_chat_id))).scalar_one_or_none() or str(op.source_chat_id);target_title=(await s.execute(select(Group.title).where(Group.chat_id==op.target_chat_id))).scalar_one_or_none() if op.target_chat_id is not None else None;target_title=target_title or op.target_title;op.status="completed";op.completed_at=now;await _delete_registered_link(s,op);notifications.append((op,source_title,target_title,source_owner,target_owner));revoke.append(op);changed+=1;continue
    if not source_ok or not target_ok:
     op.status="stopped";op.completed_at=now;await _delete_registered_link(s,op);revoke.append(op);changed+=1
 for op in revoke:await _revoke(bot,op)
 for op,source_title,target_title,source_owner,target_owner in notifications:await _notify(bot,op,source_title,target_title,source_owner,target_owner)
 async with session_factory() as s:
  active=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.status=="active",AdvertisingManualOp.target_chat_id.is_not(None)))).scalars().all())
 for op in active:
  if await _bot_admin(bot,op.target_chat_id):continue
  async with session_factory() as s:
   async with s.begin():
    locked=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==op.id,AdvertisingManualOp.status=="active").with_for_update())).scalar_one_or_none()
    if locked is not None:locked.status="stopped";locked.completed_at=now;await _delete_registered_link(s,locked);changed+=1
  await _revoke(bot,op)
 async with session_factory() as s:
  async with s.begin():changed+=await _cleanup_finished_links(s)
 return changed
async def advertising_manual_lifecycle_worker(bot:Bot,session_factory:async_sessionmaker[AsyncSession],*,interval_seconds:int=30)->None:
 while True:
  try:await run_advertising_manual_lifecycle_once(bot,session_factory)
  except asyncio.CancelledError:raise
  except Exception:logger.exception("Manual advertising lifecycle iteration failed")
  await asyncio.sleep(interval_seconds)
