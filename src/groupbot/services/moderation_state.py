from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import ChatPermissions
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from groupbot.moderation_models import ModerationAction


def _fallback_member_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )


async def group_member_permissions(bot: Bot, chat_id: int) -> ChatPermissions:
    """Return the group's current default member permissions.

    Telegram restores restricted members to the chat's default permissions. Mimorus
    must do the same when an admin removes a mute manually; hard-coded all-allowed
    permissions can accidentally bypass restrictions configured by the owner.
    """
    chat = await bot.get_chat(chat_id)
    return chat.permissions or _fallback_member_permissions()


async def restore_member_permissions(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=await group_member_permissions(bot, chat_id),
    )


async def expire_timed_moderation_actions(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Mark Telegram-expired timed moderation actions inactive in Mimorus state."""
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(ModerationAction)
                .where(
                    ModerationAction.is_active.is_(True),
                    ModerationAction.expires_at.is_not(None),
                    ModerationAction.expires_at <= now,
                )
                .values(
                    is_active=False,
                    revoked_at=ModerationAction.expires_at,
                )
            )
    return int(result.rowcount or 0)
