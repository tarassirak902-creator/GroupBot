from __future__ import annotations
import logging
from collections.abc import Awaitable,Callable
from datetime import datetime,timezone
from html import escape
from typing import Any
from aiogram import BaseMiddleware,Bot
from aiogram.types import InlineKeyboardButton,InlineKeyboardMarkup,Message,TelegramObject
from sqlalchemy import and_,or_,select
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker
from groupbot.advertising_manual_models import AdvertisingManualOp,AdvertisingManualOpCredit
from groupbot.advertising_models import AdvertisingDeal,AdvertisingListing,AdvertisingPlacement
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.models import AdminAssignment,GroupOwner,GroupSettings
from groupbot.services.users import upsert_user
logger=logging.getLogger(__name__)
async def _is_op_exempt_in_db(s:AsyncSession,cid:int,uid:int)->bool:
 if (await s.execute(select(GroupOwner.id).where(GroupOwner.chat_id==cid,GroupOwner.user_id==uid,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none() is not None:return True
 if (await s.execute(select(AdminAssignment.id).where(AdminAssignment.chat_id==cid,AdminAssignment.user_id==uid).limit(1))).scalar_one_or_none() is not None:return True
 cfg=(await s.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id==cid))).scalar_one_or_none() or {};return uid in {int(v) for v in (dict(cfg.get("special_statuses") or {}).get("vip") or []) if str(v).lstrip("-").isdigit()}
class AdvertisingMandatoryMiddleware(BaseMiddleware):
 def __init__(self,session_factory:async_sessionmaker[AsyncSession])->None:
  self.session_factory=session_factory
  from groupbot.routers.advertising_mutual_patches import install_mutual_ui_patches
  install_mutual_ui_patches()
 async def __call__(self,handler:Callable[[TelegramObject,dict[str,Any]],Awaitable[Any]],event:TelegramObject,data:dict[str,Any])->Any:
  if not isinstance(event,Message) or event.chat.type not in {"group","supergroup"} or event.from_user is None or event.from_user.is_bot:return await handler(event,data)
  bot=data.get("bot")
  if not isinstance(bot,Bot):return await handler(event,data)
  reqs=[];now=datetime.now(timezone.utc)
  async with self.session_factory() as s:
   placements=list((await s.execute(select(AdvertisingPlacement).join(AdvertisingDeal,AdvertisingDeal.id==AdvertisingPlacement.deal_id).join(AdvertisingListing,AdvertisingListing.id==AdvertisingDeal.listing_id).where(AdvertisingListing.chat_id==event.chat.id,AdvertisingPlacement.kind=="mandatory",AdvertisingPlacement.status=="active",AdvertisingDeal.status=="accepted"))).scalars().all())
   for p in placements:
    cfg=dict(p.config_json or {});target=cfg.get("target_chat_id");url=str(cfg.get("target_url") or "")
    if isinstance(target,int) and url:reqs.append({"target_chat_id":target,"url":url,"title":str(cfg.get("target_title") or "Группа")})
   mutual=list((await s.execute(select(AdvertisingMutualOpDirection).join(AdvertisingDeal,AdvertisingDeal.id==AdvertisingMutualOpDirection.deal_id).where(AdvertisingMutualOpDirection.source_chat_id==event.chat.id,AdvertisingMutualOpDirection.status=="active",AdvertisingDeal.status=="accepted"))).scalars().all())
   for d in mutual:
    if d.invite_link:reqs.append({"target_chat_id":d.target_chat_id,"url":d.invite_link,"title":d.target_title})
   manual=list((await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==event.chat.id,AdvertisingManualOp.status=="active",or_(and_(AdvertisingManualOp.mode=="days",AdvertisingManualOp.ends_at>now),and_(AdvertisingManualOp.mode=="subscribers",AdvertisingManualOp.progress_count<AdvertisingManualOp.quantity),AdvertisingManualOp.mode=="unlimited")))).scalars().all())
   for op in manual:
    credit=(await s.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==op.id,AdvertisingManualOpCredit.user_id==event.from_user.id).limit(1))).scalar_one_or_none()
    if credit is not None and credit.satisfied and credit.reason in {"restricted","join_request"}:continue
    reqs.append({"target_chat_id":op.target_chat_id,"url":op.target_url,"title":op.target_title,"manual_op_id":op.id})
   if not reqs:return await handler(event,data)
   if await _is_op_exempt_in_db(s,event.chat.id,event.from_user.id):return await handler(event,data)
  try:
   own=await bot.get_chat_member(event.chat.id,event.from_user.id)
   if own.status in {"administrator","creator"}:return await handler(event,data)
  except Exception:logger.info("Could not verify OP admin exemption chat=%s user=%s",event.chat.id,event.from_user.id)
  missing=None
  for req in reqs:
   target=req.get("target_chat_id")
   if target is None:missing=req;break
   try:member=await bot.get_chat_member(target,event.from_user.id)
   except Exception:
    logger.exception("Could not verify OP membership target=%s user=%s",target,event.from_user.id);missing=req;break
   joined=member.status in {"member","administrator","creator"} or (member.status=="restricted" and getattr(member,"is_member",True));manual_id=req.get("manual_op_id")
   if manual_id and (joined or member.status in {"kicked","banned"}):
    counted=joined
    async with self.session_factory() as s:
     async with s.begin():
      await upsert_user(s,event.from_user);op=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==manual_id,AdvertisingManualOp.status=="active").with_for_update())).scalar_one_or_none();credit=(await s.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==manual_id,AdvertisingManualOpCredit.user_id==event.from_user.id).with_for_update())).scalar_one_or_none()
      if op is not None:
       target_open=op.mode!="subscribers" or op.progress_count<op.quantity
       if credit is None:
        s.add(AdvertisingManualOpCredit(op_id=manual_id,user_id=event.from_user.id,satisfied=True,counted=counted and target_open,reason="joined" if joined else "restricted"))
        if counted and target_open and op.mode=="subscribers":op.progress_count=min(op.progress_count+1,op.quantity)
       elif joined:
        credit.satisfied=True;credit.reason="joined"
        if not credit.counted and target_open:
         credit.counted=True
         if op.mode=="subscribers":op.progress_count=min(op.progress_count+1,op.quantity)
       else:
        credit.satisfied=True;credit.reason="restricted"
    if not joined:
     try:await bot.send_message(event.chat.id,"⚠️ В Рекламной группе вы ограничены или ваша заявка на вступление была отклонена, поэтому Mimorus засчитывает вам обязательную подписку как выполненную.")
     except Exception:pass
    continue
   if manual_id and not joined:
    # Reconcile a voluntary leave here as well as in chat_member tracking. This
    # makes the next attempted message authoritative even if Telegram's member
    # update was delayed or missed: 1/2 -> 0/2, then the message is blocked.
    async with self.session_factory() as s:
     async with s.begin():
      op=(await s.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==manual_id,AdvertisingManualOp.status=="active").with_for_update())).scalar_one_or_none();credit=(await s.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==manual_id,AdvertisingManualOpCredit.user_id==event.from_user.id).with_for_update())).scalar_one_or_none()
      if op is not None and credit is not None and credit.reason=="joined":
       if credit.counted and op.mode=="subscribers":op.progress_count=max(op.progress_count-1,0)
       credit.counted=False;credit.satisfied=False;credit.reason="left"
   if not joined:missing=req;break
  if missing is None:return await handler(event,data)
  try:await bot.delete_message(event.chat.id,event.message_id)
  except Exception:logger.info("Could not delete OP-blocked message chat=%s message=%s",event.chat.id,event.message_id)
  name=event.from_user.full_name or event.from_user.username or "Пользователь";link=f'<a href="tg://user?id={event.from_user.id}">{escape(name)}</a>';title=str(missing["title"]).strip() or "Группа";title=title if len(title)<=48 else title[:47].rstrip()+"…"
  try:await bot.send_message(event.chat.id,f"👤 {link}, чтобы писать в группе, Вам необходимо подписаться на:",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🏠 {title}",url=missing["url"])]]))
  except Exception:logger.exception("Could not send OP notice chat=%s",event.chat.id)
  return None
