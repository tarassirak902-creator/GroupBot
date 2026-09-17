from aiogram.types import BotCommand


# The Telegram slash menu in groups intentionally exposes only one public entry.
# Other command handlers may still exist for backwards compatibility, but they are
# not advertised in the '/' menu.
GROUP_COMMANDS = [
    BotCommand(command="support", description="🛠 Связаться с создателем Mimorus"),
]
