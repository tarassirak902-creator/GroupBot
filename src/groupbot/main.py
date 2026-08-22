import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommandScopeAllGroupChats

from groupbot.config import get_settings
from groupbot.db import create_session_factory
from groupbot.middleware.idempotency import IdempotencyMiddleware
from groupbot.routers import creator_subscription_duration as creator_subscription_duration_module
from groupbot.routers import creator_user_profile_links as creator_user_profile_links_module
from groupbot.routers.admin_hierarchy import create_admin_hierarchy_router
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
from groupbot.routers.private import create_private_router
from groupbot.routers.user_display import clickable_user_display
from groupbot.workers.group_lifecycle import group_lifecycle_worker


async def clear_global_group_commands(bot: Bot) -> None:
    # Commands are registered per chat only after an owner activates a tariff.
    # This also clears any global group command menu left from an older build.
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Single public identity format: clickable name | clickable @username.
    # Numeric Telegram ids remain internal keys only.
    creator_subscription_duration_module._user_link = clickable_user_display
    creator_user_profile_links_module._user_link = clickable_user_display

    bot = Bot(settings.bot_token)
    session_factory = create_session_factory(settings)
    dp = Dispatcher()
    dp.update.outer_middleware(IdempotencyMiddleware(session_factory))
    dp.include_router(create_group_router(session_factory))
    # Privacy router is intentionally first among user-facing feature routers:
    # it prevents numeric Telegram ids from leaking into current UI screens and
    # lets owner-side assignments select people by human-readable identity.
    dp.include_router(create_identity_privacy_router(session_factory, settings))
    dp.include_router(create_group_commands_router(session_factory))
    # Fixed standard hierarchy and assignment limits go before the generic
    # role UX so standard rank screens/assignments are handled deterministically.
    dp.include_router(create_admin_hierarchy_router(session_factory))
    # UX overrides: mode descriptions stay visible and custom-role permissions
    # are edited as a draft, then applied only by the explicit Save button.
    dp.include_router(create_group_control_ux_router(session_factory))
    # Remaining role actions (for example role enable/disable) keep working.
    dp.include_router(create_group_control_role_actions_router(session_factory))
    # Real owner-side moderation/administration screens intercept the generic
    # group section callbacks before private.py's fallback placeholder.
    dp.include_router(create_group_control_router(session_factory))
    # Duration presets intercept creator subscription assignment before the
    # generic creator handler, so paid tariffs offer fast 7/15/30-day choices.
    dp.include_router(create_creator_subscription_duration_router(session_factory, settings))
    # Creator subscription fallback screens also use the same private identity
    # policy (tariff choice and cancel confirmation/results).
    dp.include_router(create_creator_identity_privacy_router(session_factory, settings))
    # Human-friendly creator user screens are also registered before the
    # generic creator router. They keep telegram_user_id internally while
    # rendering clickable tg://user links in the interface.
    dp.include_router(create_creator_user_profile_links_router(session_factory, settings))
    # Group cards use live Telegram metadata so title/username links stay current.
    dp.include_router(create_creator_group_profile_links_router(session_factory, settings))
    # Creator router goes before the generic private router so the creator-only
    # menu button is handled by the real global panel rather than a placeholder.
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
