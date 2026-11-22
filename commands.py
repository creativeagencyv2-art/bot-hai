from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

from app import config

# The list shown when a customer taps the "Menu" button next to the message box.
PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Open the store menu"),
    BotCommand(command="products", description="Browse all products"),
    BotCommand(command="wallet", description="Wallet balance & top up"),
    BotCommand(command="orders", description="Your orders"),
]


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(PUBLIC_COMMANDS)
    # Admins also get an extra entry. Works once the admin has opened the bot at least once.
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(PUBLIC_COMMANDS + [BotCommand(command="admin", description="Admin panel")],
                                      scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass
