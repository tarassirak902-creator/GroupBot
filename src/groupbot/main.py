import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommandScopeAllGroupChats

from groupbot.config import get_settings
from groupbot.db import create_session_factory
from groupbot.middleware.antiflood import AntiFloodMiddleware
from groupbot.middleware.antispam import AntiSpamMiddleware
from groupbot.middleware.idempotency import IdempotencyMiddleware
from groupbot.middleware.member_tracking import GroupMemberTrackingMiddleware
from groupbot.routers import creator_subscription_duration as creator_subscription_duration_module
from groupbot.routers import creator_user_profile_links as creator_user_profile_links_module
from groupbot.routers.admin_hierarchy import create_admin_hierarchy_router
from groupbot.routers.admin_member_sync import create_admin_member_sync_router
from groupbot.routers.admins_display import create_admins_display_router
from groupbot.routers.antiflood import create_antiflood_router
from groupbot.routers.antispam import create_antispam_router
from groupbot.routers.ban_cleanup import create_ban_cleanup_router
from groupbot.routers.creator import create_creator_router
from groupbot.routers.creator_group_profile_links import create_creator_group_profile_links_router
from groupbot.routers.creator_identity_privacy import create_creator_identity_privacy_router
from groupbot.routers.creator_subscription_duration import create_creator_subscription_duration_router
from groupbot.routers.creator_user_profile_links import create_creator_user_profile_links_router
from groupbot.routers.group_commands import create_group_commands_router
from groupbot.routers.group_control import create_group_control_router
from groupbot.routers.group_control_role_actions import create_group_control_role_actions_router
from groupbot.routers.group_control_ux import create_group_control_ux_router
from groupbot.routers.groups import create_group_router
from groupbot.routers.identity_privacy import create_identity_privacy_router
from groupbot.routers.manual_moderation import create_manual_moderation_router
from groupbot.routers.message_operations import create_message_operations_router
from groupbot.routers.private import create_private_router
from groupbot.routers.punishment_reasons import create_punishment_reasons_router
from groupbot.routers.user_display import clickable_user_display
from groupbot.workers.group_lifecycle import group_lifecycle_worker


async def clear_global_group_commands(bot: Bot) -> None:
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    creator_subscription_duration_module._user_link = clickable_user_display
    creator_user_profile_links_module._user_link = clickable_user_display

    bot = Bot(settings.bot_token)
    session_factory = create_session_factory(settings)
    dp = Dispatcher()
    dp.update.outer_middleware(IdempotencyMiddleware(session_factory))
    # Persist the current group message first; protection middleware then sees it
    # in observed_messages before ordinary routers process the update.
    dp.message.outer_middleware(GroupMemberTrackingMiddleware(session_factory))
    dp.message.outer_middleware(AntiFloodMiddleware(session_factory))
    dp.message.outer_middleware(AntiSpamMiddleware(session_factory))

    dp.include_router(create_group_router(session_factory))
    dp.include_router(create_admin_member_sync_router(session_factory))
    dp.include_router(create_admins_display_router(session_factory))
    dp.include_router(create_identity_privacy_router(session_factory, settings))
    dp.include_router(create_message_operations_router(session_factory))
    dp.include_router(create_ban_cleanup_router(session_factory))
    dp.include_router(create_manual_moderation_router(session_factory))
    dp.include_router(create_group_commands_router(session_factory))
    dp.include_router(create_admin_hierarchy_router(session_factory))
    dp.include_router(create_group_control_ux_router(session_factory))
    dp.include_router(create_group_control_role_actions_router(session_factory))
    dp.include_router(create_punishment_reasons_router(session_factory))
    dp.include_router(create_antiflood_router(session_factory))
    dp.include_router(create_antispam_router(session_factory))
    dp.include_router(create_group_control_router(session_factory))
    dp.include_router(create_creator_subscription_duration_router(session_factory, settings))
    dp.include_router(create_creator_identity_privacy_router(session_factory, settings))
    dp.include_router(create_creator_user_profile_links_router(session_factory, settings))
    dp.include_router(create_creator_group_profile_links_router(session_factory, settings))
    dp.include_router(create_creator_router(session_factory, settings))
    dp.include_router(create_private_router(session_factory, settings))

    await clear_global_group_commands(bot)
    lifecycle_task = asyncio.create_task(group_lifecycle_worker(bot, session_factory))
    try:
        await dp.start_polling(bot)
    finally:
        lifecycle_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
