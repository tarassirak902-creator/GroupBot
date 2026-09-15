from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.advertising_manual_models import AdvertisingManualOp, AdvertisingManualOpCredit
from groupbot.advertising_models import AdvertisingDeal, AdvertisingListing, AdvertisingPlacement
from groupbot.advertising_mutual_models import AdvertisingMutualOpDirection
from groupbot.models import AdminAssignment, GroupOwner, GroupSettings
from groupbot.services.users import upsert_user

logger = logging.getLogger(__name__)

async def _is_op_exempt_in_db(session:AsyncSession,chat_id:int,user_id:int)->bool:
    owner=(await session.execute(select(GroupOwner.id).where(GroupOwner.chat_id==chat_id,GroupOwner.user_id==user_id,GroupOwner.is_current.is_(True)).limit(1))).scalar_one_or_none()
    if owner is not None:return True
    admin=(await session.execute(select(AdminAssignment.id).where(AdminAssignment.chat_id==chat_id,AdminAssignment.user_id==user_id).limit(1))).scalar_one_or_none()
    if admin is not None:return True
    cfg=(await session.execute(select(GroupSettings.moderation_config).where(GroupSettings.chat_id==chat_id))).scalar_one_or_none() or {}
    return user_id in {int(v) for v in (dict(cfg.get("special_statuses") or {}).get("vip") or []) if str(v).lstrip("-").isdigit()}

class AdvertisingMandatoryMiddleware(BaseMiddleware):
    def __init__(self,session_factory:async_sessionmaker[AsyncSession])->None:
        self.session_factory=session_factory
        from groupbot.routers.advertising_mutual_patches import install_mutual_ui_patches
        install_mutual_ui_patches()

    async def __call__(self,handler:Callable[[TelegramObject,dict[str,Any]],Awaitable[Any]],event:TelegramObject,data:dict[str,Any])->Any:
        if not isinstance(event,Message) or event.chat.type not in {"group","supergroup"} or event.from_user is None or event.from_user.is_bot:return await handler(event,data)
        bot=data.get("bot")
        if not isinstance(bot,Bot):return await handler(event,data)
        requirements:list[dict[str,Any]]=[]
        async with self.session_factory() as session:
            placements=list((await session.execute(select(AdvertisingPlacement).join(AdvertisingDeal,AdvertisingDeal.id==AdvertisingPlacement.deal_id).join(AdvertisingListing,AdvertisingListing.id==AdvertisingDeal.listing_id).where(AdvertisingListing.chat_id==event.chat.id,AdvertisingPlacement.kind=="mandatory",AdvertisingPlacement.status=="active",AdvertisingDeal.status=="accepted").order_by(AdvertisingPlacement.starts_at,AdvertisingPlacement.id))).scalars().all())
            for placement in placements:
                cfg=dict(placement.config_json or {});target=cfg.get("target_chat_id");url=str(cfg.get("target_url") or "")
                if isinstance(target,int) and url:requirements.append({"target_chat_id":target,"url":url,"title":str(cfg.get("target_title") or cfg.get("target_username") or "Группа")})
            mutual=list((await session.execute(select(AdvertisingMutualOpDirection).join(AdvertisingDeal,AdvertisingDeal.id==AdvertisingMutualOpDirection.deal_id).where(AdvertisingMutualOpDirection.source_chat_id==event.chat.id,AdvertisingMutualOpDirection.status=="active",AdvertisingDeal.status=="accepted").order_by(AdvertisingMutualOpDirection.starts_at,AdvertisingMutualOpDirection.id))).scalars().all())
            for direction in mutual:
                if direction.invite_link:requirements.append({"target_chat_id":direction.target_chat_id,"url":direction.invite_link,"title":direction.target_title})
            manual=list((await session.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.source_chat_id==event.chat.id,AdvertisingManualOp.status=="active").order_by(AdvertisingManualOp.id))).scalars().all())
            for op in manual:
                credited=(await session.execute(select(AdvertisingManualOpCredit.id).where(AdvertisingManualOpCredit.op_id==op.id,AdvertisingManualOpCredit.user_id==event.from_user.id,AdvertisingManualOpCredit.satisfied.is_(True)).limit(1))).scalar_one_or_none()
                if credited is None:requirements.append({"target_chat_id":op.target_chat_id,"url":op.target_url,"title":op.target_title,"manual_op_id":op.id})
            if not requirements:return await handler(event,data)
            if await _is_op_exempt_in_db(session,event.chat.id,event.from_user.id):return await handler(event,data)
        try:
            own=await bot.get_chat_member(event.chat.id,event.from_user.id)
            if own.status in {"administrator","creator"}:return await handler(event,data)
        except Exception:logger.info("Could not verify Telegram admin exemption chat_id=%s user_id=%s",event.chat.id,event.from_user.id)

        missing=None
        for req in requirements:
            target=req.get("target_chat_id")
            if target is None:
                missing=req;break
            try:member=await bot.get_chat_member(target,event.from_user.id)
            except Exception:
                logger.exception("Could not verify advertising OP membership target_chat_id=%s user_id=%s",target,event.from_user.id);continue
            joined=member.status in {"member","administrator","creator"} or (member.status=="restricted" and getattr(member,"is_member",True))
            manual_id=req.get("manual_op_id")
            if manual_id and (joined or member.status in {"kicked","banned"}):
                counted=joined
                async with self.session_factory() as session:
                    async with session.begin():
                        await upsert_user(session,event.from_user)
                        op=(await session.execute(select(AdvertisingManualOp).where(AdvertisingManualOp.id==manual_id,AdvertisingManualOp.status=="active").with_for_update())).scalar_one_or_none()
                        credit=(await session.execute(select(AdvertisingManualOpCredit).where(AdvertisingManualOpCredit.op_id==manual_id,AdvertisingManualOpCredit.user_id==event.from_user.id).with_for_update())).scalar_one_or_none()
                        if op is not None and credit is None:
                            session.add(AdvertisingManualOpCredit(op_id=manual_id,user_id=event.from_user.id,satisfied=True,counted=counted,reason="joined" if joined else "restricted"))
                            if counted and op.mode=="subscribers":op.progress_count+=1
                if not joined:
                    try:await bot.send_message(event.chat.id,"⚠️ В Рекламной группе вы ограничены или ваша заявка на вступление была отклонена, поэтому Mimorus засчитывает вам обязательную подписку как выполненную.")
                    except Exception:pass
                continue
            if not joined:missing=req;break
        if missing is None:return await handler(event,data)
        try:await bot.delete_message(event.chat.id,event.message_id)
        except Exception:logger.info("Could not delete message blocked by advertising OP chat_id=%s message_id=%s",event.chat.id,event.message_id)
        name=event.from_user.full_name or event.from_user.username or "Пользователь";user_link=f'<a href="tg://user?id={event.from_user.id}">{escape(name)}</a>';title=str(missing["title"]).strip() or "Группа";title=title if len(title)<=48 else title[:47].rstrip()+"…"
        try:await bot.send_message(event.chat.id,f"👤 {user_link}, чтобы писать в группе, Вам необходимо подписаться на:",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🏠 {title}",url=missing["url"])]]))
        except Exception:logger.exception("Could not send advertising OP notice chat_id=%s",event.chat.id)
        return None
