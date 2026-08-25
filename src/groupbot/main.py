import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import BotCommandScopeAllGroupChats

from groupbot.config import get_settings
from groupbot.db import create_session_factory
from groupbot.middleware import antiflood as antiflood_middleware_module
from groupbot.middleware import antilinks as antilinks_middleware_module
from groupbot.middleware import antispam as antispam_middleware_module
from groupbot.middleware import content_filters as content_filters_middleware_module
from groupbot.middleware.antiflood import AntiFloodMiddleware
from groupbot.middleware.antilinks import AntiLinksMiddleware
from groupbot.middleware.antispam import AntiSpamMiddleware
from groupbot.middleware.content_filters import ContentFiltersMiddleware
from groupbot.middleware.idempotency import IdempotencyMiddleware
from groupbot.middleware.member_tracking import GroupMemberTrackingMiddleware
from groupbot.routers import advertising as advertising_module
from groupbot.routers import advertising_edit as advertising_edit_module
from groupbot.routers import advertising_edit_types as advertising_edit_types_module
from groupbot.routers import advertising_post_request as advertising_post_request_module
from groupbot.routers import ban_cleanup as ban_cleanup_module
from groupbot.routers import creator_subscription_duration as creator_subscription_duration_module
from groupbot.routers import creator_user_profile_links as creator_user_profile_links_module
from groupbot.routers import entry_protection as entry_protection_module
from groupbot.routers import manual_moderation as manual_moderation_module
from groupbot.routers.admin_hierarchy import create_admin_hierarchy_router
from groupbot.routers.admin_member_sync import create_admin_member_sync_router
from groupbot.routers.admins_display import create_admins_display_router
from groupbot.routers.advertising import create_advertising_router
from groupbot.routers.advertising_deal_actions_v2 import create_advertising_deal_actions_v2_router
from groupbot.routers.advertising_edit import create_advertising_edit_router
from groupbot.routers.advertising_edit_types import create_advertising_edit_types_router
from groupbot.routers.advertising_materials import create_advertising_materials_router
from groupbot.routers.advertising_mimorus_post import create_advertising_mimorus_post_router
from groupbot.routers.advertising_post_duration import (
    create_advertising_post_duration_router,
    editor_keyboard_with_duration,
    listing_text_with_duration,
    post_editor_keyboard_with_cancel,
)
from groupbot.routers.advertising_post_request import create_advertising_post_request_router
from groupbot.routers.advertising_requests import create_advertising_requests_router
from groupbot.routers.antiflood import create_antiflood_router
from groupbot.routers.antilinks import create_antilinks_router
from groupbot.routers.antispam import create_antispam_router
from groupbot.routers.ban_cleanup import create_ban_cleanup_router
from groupbot.routers.content_filters import create_content_filters_router
from groupbot.routers.creator import create_creator_router
from groupbot.routers.creator_group_profile_links import create_creator_group_profile_links_router
from groupbot.routers.creator_identity_privacy import create_creator_identity_privacy_router
from groupbot.routers.creator_subscription_duration import create_creator_subscription_duration_router
from groupbot.routers.creator_user_profile_links import create_creator_user_profile_links_router
from groupbot.routers.entry_protection import create_entry_protection_router
from groupbot.routers.group_analytics import create_group_analytics_router
from groupbot.routers.group_cabinet_actions import create_group_cabinet_actions_router
from groupbot.routers.group_commands import create_group_commands_router
from groupbot.routers.group_control import create_group_control_router
from groupbot.routers.group_control_role_actions import create_group_control_role_actions_router
from groupbot.routers.group_control_ux import create_group_control_ux_router
from groupbot.routers.group_profile_stats import create_group_profile_stats_router
from groupbot.routers.group_sections_nav import create_group_sections_nav_router
from groupbot.routers.group_startgroup import create_group_startgroup_router
from groupbot.routers.group_text_aliases import create_group_text_aliases_router
from groupbot.routers.groups import create_group_router
from groupbot.routers.identity_privacy import create_identity_privacy_router
from groupbot.routers.manual_moderation import create_manual_moderation_router
from groupbot.routers.message_operations import create_message_operations_router
from groupbot.routers.moderation_release import create_moderation_release_router
from groupbot.routers.network_admins import create_network_admins_router
from groupbot.routers.network_moderation import create_network_moderation_router
from groupbot.routers.networks import create_networks_router
from groupbot.routers.private import create_private_router
from groupbot.routers.protection_schedule import create_protection_schedule_router
from groupbot.routers.punishment_reasons import create_punishment_reasons_router
from groupbot.routers.reserve_admin import create_reserve_admin_router
from groupbot.routers.special_status_members import create_special_status_members_router
from groupbot.routers.subscription_payments import create_subscription_payments_router
from groupbot.routers.tariff_limits import create_tariff_limits_router
from groupbot.routers.user_display import clickable_user_display
from groupbot.services.default_punishment_reasons import configured_reasons_with_defaults
from groupbot.services.entry_schedule_adapter import install_entry_schedule
from groupbot.services.moderation_notifications import unified_execute_action
from groupbot.workers.advertising_lifecycle import advertising_lifecycle_worker
from groupbot.workers.group_lifecycle import group_lifecycle_worker
from groupbot.workers.subscription_lifecycle import subscription_lifecycle_worker


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

    manual_moderation_module._execute_action = unified_execute_action
    ban_cleanup_module._execute_action = unified_execute_action
    antiflood_middleware_module._execute_action = unified_execute_action
    antispam_middleware_module._execute_action = unified_execute_action
    antilinks_middleware_module._execute_action = unified_execute_action
    content_filters_middleware_module._execute_action = unified_execute_action

    manual_moderation_module._configured_reasons = configured_reasons_with_defaults
    manual_moderation_module.ACTION_ALIASES["варн"] = "warning"
    ban_cleanup_module._configured_reasons = configured_reasons_with_defaults

    advertising_module._listing_text = listing_text_with_duration
    advertising_edit_module._listing_text = listing_text_with_duration
    advertising_edit_module._editor_keyboard = editor_keyboard_with_duration
    advertising_edit_types_module._listing_text = listing_text_with_duration
    advertising_edit_types_module._editor_keyboard = editor_keyboard_with_duration
    advertising_post_request_module._editor_keyboard = post_editor_keyboard_with_cancel

    install_entry_schedule(entry_protection_module)

    bot = Bot(settings.bot_token)
    session_factory = create_session_factory(settings)
    dp = Dispatcher()
    dp.update.outer_middleware(IdempotencyMiddleware(session_factory))
    dp.message.outer_middleware(GroupMemberTrackingMiddleware(session_factory))
    dp.message.outer_middleware(ContentFiltersMiddleware(session_factory))
    dp.message.outer_middleware(AntiFloodMiddleware(session_factory))
    dp.message.outer_middleware(AntiSpamMiddleware(session_factory))
    dp.message.outer_middleware(AntiLinksMiddleware(session_factory))

    dp.include_router(create_group_startgroup_router(session_factory))
    dp.include_router(create_group_router(session_factory))
    dp.include_router(create_admin_member_sync_router(session_factory))
    dp.include_router(create_admins_display_router(session_factory))
    dp.include_router(create_identity_privacy_router(session_factory, settings))
    dp.include_router(create_network_moderation_router(session_factory))
    dp.include_router(create_message_operations_router(session_factory))
    dp.include_router(create_ban_cleanup_router(session_factory))
    dp.include_router(create_moderation_release_router(session_factory))

    manual_router = create_manual_moderation_router(session_factory)
    manual_router.message.filter(
        F.chat.type.in_({"group", "supergroup"}),
        F.text.regexp(r"(?i)^\s*(?:(?:пред|варн|мут|бан|размут|разбан)(?:\s+.*)?|мои\s+баны|мои\s+муты|выдал\s+пред|банлист|мутлист|преды)\s*$"),
    )
    dp.include_router(manual_router)

    dp.include_router(create_group_text_aliases_router(session_factory))
    dp.include_router(create_group_profile_stats_router(session_factory))
    dp.include_router(create_group_analytics_router(session_factory))
    dp.include_router(create_group_commands_router(session_factory))
    dp.include_router(create_special_status_members_router(session_factory))
    dp.include_router(create_admin_hierarchy_router(session_factory))
    dp.include_router(create_group_control_ux_router(session_factory))
    dp.include_router(create_group_control_role_actions_router(session_factory))
    dp.include_router(create_punishment_reasons_router(session_factory))
    dp.include_router(create_tariff_limits_router(session_factory))
    dp.include_router(create_antiflood_router(session_factory))
    dp.include_router(create_antispam_router(session_factory))
    dp.include_router(create_antilinks_router(session_factory))
    dp.include_router(create_content_filters_router(session_factory))
    dp.include_router(create_entry_protection_router(session_factory))
    dp.include_router(create_protection_schedule_router(session_factory))
    dp.include_router(create_reserve_admin_router(session_factory))
    dp.include_router(create_network_admins_router(session_factory))
    dp.include_router(create_group_sections_nav_router(session_factory))
    dp.include_router(create_group_control_router(session_factory))
    dp.include_router(create_networks_router(session_factory))
    dp.include_router(create_creator_subscription_duration_router(session_factory, settings))
    dp.include_router(create_creator_identity_privacy_router(session_factory, settings))
    dp.include_router(create_creator_user_profile_links_router(session_factory, settings))
    dp.include_router(create_creator_group_profile_links_router(session_factory, settings))
    dp.include_router(create_advertising_post_duration_router(session_factory))
    dp.include_router(create_advertising_edit_types_router(session_factory))
    dp.include_router(create_advertising_edit_router(session_factory))
    dp.include_router(create_advertising_post_request_router(session_factory))
    dp.include_router(create_advertising_materials_router(session_factory))
    dp.include_router(create_advertising_deal_actions_v2_router(session_factory))
    dp.include_router(create_advertising_requests_router(session_factory))
    dp.include_router(create_advertising_mimorus_post_router(session_factory))
    dp.include_router(create_advertising_router(session_factory, settings))
    dp.include_router(create_creator_router(session_factory, settings))
    dp.include_router(create_subscription_payments_router(session_factory))
    dp.include_router(create_group_cabinet_actions_router(session_factory))
    dp.include_router(create_private_router(session_factory, settings))

    await clear_global_group_commands(bot)
    lifecycle_task = asyncio.create_task(group_lifecycle_worker(bot, session_factory))
    subscription_lifecycle_task = asyncio.create_task(subscription_lifecycle_worker(session_factory))
    advertising_lifecycle_task = asyncio.create_task(advertising_lifecycle_worker(bot, session_factory))
    try:
        await dp.start_polling(bot)
    finally:
        lifecycle_task.cancel()
        subscription_lifecycle_task.cancel()
        advertising_lifecycle_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
