"""Posts a short announcement to the sales channel after each purchase.
Shows: product, selling price, masked buyer name, time. NEVER shows cost, profit, delivered items or contact details."""
import html
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import config
from app.db import SessionLocal
from app.models import Order, User

log = logging.getLogger("feed")


def _inr(paise: int) -> str:
    return f"₹{paise // 100:,}" if paise % 100 == 0 else f"₹{paise / 100:,.2f}"


def mask_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "Customer"
    first = name.split()[0]
    return first[0] + "***" + (first[-1] if len(first) > 2 else "")


def build_post(product: str, price_paise: int, buyer: str, when, order_id: int | None = None, bot_username: str | None = None) -> str:
    ist = when + timedelta(hours=5, minutes=30)  # stored in UTC
    lines = [
        "🛒 <b>New Purchase!</b>", "",
        f"📦 <b>{html.escape(product)}</b>",
        f"💵 Price: <b>{_inr(price_paise)}</b>",
        f"👤 Buyer: {html.escape(buyer)}",
        f"🕐 {ist:%d %b %Y, %I:%M %p} IST",
    ]
    if order_id:
        lines.append(f"🧾 Order #{order_id}")
    if bot_username:
        lines += ["", f"🛍️ Buy here: @{bot_username}"]
    return "\n".join(lines)


async def _username(bot) -> str | None:
    try:
        return (await bot.me()).username
    except Exception:
        return None


async def post_sale(bot, order_id: int) -> None:
    """Never raises: a feed problem must never affect a customer's order."""
    if not config.SALES_CHANNEL:
        return
    try:
        async with SessionLocal() as s:
            o = (await s.execute(select(Order).options(selectinload(Order.plan)).where(Order.id == order_id))).scalar_one()
            u = await s.get(User, o.user_id)
        text = build_post(o.plan.name, o.sell_price_paise, mask_name(u.full_name if u else ""), o.created_at, o.id, await _username(bot))
        await bot.send_message(config.SALES_CHANNEL, text, parse_mode="HTML")
    except Exception:
        log.exception("could not post to the sales channel")


async def send_test(bot) -> str | None:
    """Returns None on success, otherwise a readable error."""
    from datetime import datetime

    try:
        text = build_post("Sample Product – 1 Month", 14900, "V***h", datetime.utcnow(), None, await _username(bot))
        await bot.send_message(config.SALES_CHANNEL, "🧪 <i>Test post</i>\n\n" + text, parse_mode="HTML")
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
