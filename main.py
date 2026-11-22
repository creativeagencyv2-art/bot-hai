import asyncio
import logging

from aiogram import Bot, Dispatcher

from app import config
from app.bot import admin, pay_flow
from app.bot.handlers import router
from app.bot.commands import setup_commands
from app.bot.force_join import ForceJoin
from app.db import init_db
from app.services import gate, poller
from app.services.catalog import seed_catalog
from app.supplier import USING_REAL
from app import webhook


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not config.BOT_TOKEN or "paste_your" in config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Open the .env file and paste your NEW BotFather token.")
    await init_db()
    await seed_catalog()
    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher()
    dp.message.outer_middleware(ForceJoin())
    dp.callback_query.outer_middleware(ForceJoin())
    dp.include_router(router)
    dp.include_router(pay_flow.router)
    dp.include_router(admin.router)
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_commands(bot)
    await gate.startup_check(bot)
    if USING_REAL:
        asyncio.create_task(poller.run(bot))
    await webhook.start(bot)
    print("Supplier:", "Silvex (LIVE - real money)" if USING_REAL else "DUMMY (test mode)")
    print("Bot is running. Press Ctrl+C to stop.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
