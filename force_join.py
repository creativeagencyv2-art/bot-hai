from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message

from app import config
from app.bot.keyboards import kb
from app.services import gate


def gate_text() -> str:
    return "🔒 <b>Join our channel to continue</b>\n\nPlease join the channel below, then tap <b>✅ I've Joined</b>."


def gate_markup(missing: list):
    rows = []
    for i, (chat, link) in enumerate(missing, 1):
        if link:
            label = "📢 Join Our Channel" if len(config.FORCE_JOIN) == 1 else f"📢 Join Channel {i}"
            rows.append([Btn(text=label, url=link)])
    rows.append([Btn(text="✅ I've Joined", callback_data="joined")])
    return kb(rows)


class ForceJoin(BaseMiddleware):
    """Runs before every handler. Blocks everything until the user has joined the required channel(s)."""

    async def __call__(self, handler, event, data):
        if not config.FORCE_JOIN:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None or user.id in config.ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "joined":
            return await handler(event, data)  # that handler re-checks by itself
        bot = data["bot"]
        missing, unknown = await gate.missing_channels(bot, user.id)
        for chat in unknown:  # misconfigured channel: do not lock customers out, but tell the admins once
            if gate.first_alert(chat):
                from app.bot.handlers import alert_admins
                await alert_admins(bot, f"⚠️ Force-join cannot check <code>{chat}</code>. Add the bot as an <b>admin</b> of that channel. Until then customers are NOT blocked.")
        if not missing:
            return await handler(event, data)
        target = event.message if isinstance(event, CallbackQuery) else event
        await target.answer(gate_text(), reply_markup=gate_markup(missing), parse_mode="HTML")
        if isinstance(event, CallbackQuery):
            await event.answer()
        return None
