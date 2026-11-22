"""Run: python -m tests.test_bot_flow
Feeds fake Telegram updates through the REAL handlers (no network, dummy supplier)."""
import asyncio
import os
import tempfile
from datetime import datetime

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/t.db"
os.environ["ADMIN_IDS"] = "5"
os.environ["UPI_ID"] = "shop@ibl"
os.environ["SILVEX_API_KEY"] = ""
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["SALES_CHANNEL"] = "-1001234567890"
os.environ["FORCE_JOIN_CHANNELS"] = "@mychan,-1001111|https://t.me/+abc"

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage, SendPhoto, SetMyCommands  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User as TgUser  # noqa: E402

from app.bot import admin, pay_flow  # noqa: E402
from app.bot.force_join import ForceJoin  # noqa: E402
from app.services import gate  # noqa: E402
from app.bot.commands import setup_commands  # noqa: E402
from app.bot.handlers import router  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.models import User  # noqa: E402
from app.services import catalog, wallet  # noqa: E402

LOG = []  # (kind, chat_id, text, reply_markup)


class MockSession(BaseSession):
    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendDocument):
            LOG.append(("doc", method.chat_id, method.caption or "", method.document.data.decode()))
            return Message(message_id=len(LOG), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text="doc")
        if isinstance(method, (SendMessage, SendPhoto)):
            text = getattr(method, "text", None) or getattr(method, "caption", "") or ""
            LOG.append(("send", method.chat_id, text, method.reply_markup))
            return Message(message_id=len(LOG), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=text)
        if isinstance(method, EditMessageText):
            LOG.append(("edit", method.chat_id, method.text, method.reply_markup))
            return True
        if isinstance(method, SetMyCommands):
            LOG.append(("cmds", None, "|".join(f"{c.command}:{c.description}" for c in method.commands), None))
            return True
        if isinstance(method, AnswerCallbackQuery):
            LOG.append(("answer", None, method.text or "", method.show_alert))
            return True
        LOG.append(("other", None, "", None))
        return True


U = TgUser(id=5, is_bot=False, first_name="Vikash", username="vik")
_mid = [100]


def msg(text, entities=None, user=None):
    user = user or U
    _mid[0] += 1
    return Message(message_id=_mid[0], date=datetime.now(), chat=Chat(id=user.id, type="private"), from_user=user, text=text, entities=entities)


async def say(dp, bot, text, user=None):
    ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
    await dp.feed_update(bot, Update(update_id=_mid[0], message=msg(text, ents, user)))


async def tap(dp, bot, data, user=None):
    cq = CallbackQuery(id="1", from_user=user or U, chat_instance="c", data=data, message=msg("old", user=user))
    await dp.feed_update(bot, Update(update_id=_mid[0], callback_query=cq))


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] if markup else []


def last(kind=None):
    items = [x for x in LOG if kind is None or x[0] == kind]
    return items[-1]


async def main():
    await init_db()
    await catalog.seed_catalog()
    bot = Bot("123456:TEST", session=MockSession())
    dp = Dispatcher()
    dp.message.outer_middleware(ForceJoin())
    dp.callback_query.outer_middleware(ForceJoin())
    dp.include_router(router)
    dp.include_router(pay_flow.router)
    dp.include_router(admin.router)

    await setup_commands(bot)
    shown = [x[2] for x in LOG if x[0] == "cmds"][0]
    assert shown == "start:Open the store menu|products:Browse all products|wallet:Wallet balance & top up|orders:Your orders", shown
    print("OK Menu button lists exactly: start, products, wallet, orders")

    await say(dp, bot, "/products")
    assert "AI Subscriptions" in last("send")[2]
    await say(dp, bot, "/wallet")
    assert "My Wallet" in last("send")[2]
    await say(dp, bot, "/orders")
    assert "no orders" in last("send")[2]
    print("OK /products, /wallet and /orders commands work")

    await say(dp, bot, "/start")
    assert "Welcome" in last("send")[2]
    await tap(dp, bot, "menu:buy")
    items = buttons(last("edit")[3])
    names = " ".join(t for t, _ in items)
    for brand in ("Canva", "Hotstar", "Netflix", "YouTube", "Gemini", "Apple Music", "Gaana"):
        assert brand in names, brand
    assert "Page 1 of 1" in last("edit")[2] and any("Search" in t for t, _ in items) and not any("Next" in t for t, _ in items)
    print("OK /start and flat product list show every product with price")

    plan_cb = next(d for t, d in items if "₹99" in t)
    await tap(dp, bot, plan_cb)
    text = last("edit")[2]
    assert "Price: <b>₹99</b>" in text and "Manual" in text and "About" in text and "Important notes" in text
    print("OK plan page: price, delivery type, about, notes")

    # ---- pagination + search with 12 extra products ----
    async with SessionLocal() as s_, s_.begin():
        from app.models import Plan, Product
        pr = Product(name="Bulk", emoji="📦"); s_.add(pr); await s_.flush()
        for i in range(12):
            s_.add(Plan(product_id=pr.id, name=f"Extra Tool {i:02d}", sell_price_paise=5000 + i * 100, cost_paise=1000, stock=i))
    await tap(dp, bot, "menu:buy")
    t1 = last("edit")[2]
    b1 = buttons(last("edit")[3])
    assert "Page 1 of 3" in t1 and sum(1 for t, d in b1 if d.startswith("plan:")) == 10
    assert any("Next Page" in t for t, _ in b1) and not any("Previous" in t for t, _ in b1)
    await tap(dp, bot, next(d for t, d in b1 if "Next Page" in t))
    b2 = buttons(last("edit")[3])
    assert "Page 2 of 3" in last("edit")[2] and any("Previous Page" in t for t, _ in b2) and any("Next Page" in t for t, _ in b2)
    await tap(dp, bot, next(d for t, d in b2 if d.startswith("plan:")))  # open an item from page 2
    back = next(d for t, d in buttons(last("edit")[3]) if "Back to Products" in t)
    assert back == "pg:1"
    await tap(dp, bot, back)
    assert "Page 2 of 3" in last("edit")[2]
    await tap(dp, bot, "pg:2")
    assert "Page 3 of 3" in last("edit")[2] and not any("Next Page" in t for t, _ in buttons(last("edit")[3]))
    await tap(dp, bot, "search")
    await say(dp, bot, "gem ini")
    res = buttons(last("send")[3])
    assert "Results for" in last("send")[2] and sum(1 for t, d in res if d.startswith("plan:")) == 2 and all("Gemini" in t for t, d in res if d.startswith("plan:"))
    await tap(dp, bot, "search")
    await say(dp, bot, "zzzz")
    assert "No products found" in last("send")[2]
    async with SessionLocal() as s_, s_.begin():
        for pl in (await s_.execute(select(Plan).where(Plan.name.like("Extra Tool%")))).scalars():
            pl.active = False
    print("OK pagination (Next/Previous/Back keeps the page) and Search work")

    await tap(dp, bot, plan_cb)  # reopen the YouTube invite plan
    buy_cb = next(d for t, d in buttons(last("edit")[3]) if "Buy Now" in t)
    await tap(dp, bot, buy_cb)
    assert "email" in last("send")[2].lower()
    await say(dp, bot, "not-an-email")
    assert "valid email" in last("send")[2]
    await say(dp, bot, "me@gmail.com")
    assert "Insufficient" in last("send")[2]
    print("OK invite plan asks for email, validates it, checks balance")

    async with SessionLocal() as s, s.begin():
        await wallet.credit(s, 5, 100000, "admin_adjust", "t")
    await tap(dp, bot, buy_cb)  # email already stored for this plan
    conf = last("edit")[2]
    assert "me@gmail.com" in conf and "Confirm" in conf
    do_cb = next(d for t, d in buttons(last("edit")[3]) if "Confirm Purchase" in t)
    await tap(dp, bot, do_cb)
    await tap(dp, bot, do_cb)  # double tap
    sends = " ".join(x[2] for x in LOG)
    assert sends.count("New manual order #1") == 1 and "Order #1 received" in sends
    async with SessionLocal() as s:
        assert (await s.get(User, 5)).balance_paise == 100000 - 9900
    print("OK invite purchase: charged once, admin notified, double-tap safe")

    await tap(dp, bot, "mo:done:1")
    assert "Order #1 is done" in " ".join(x[2] for x in LOG)
    print("OK admin 'Delivered' notifies the customer")

    await tap(dp, bot, "menu:buy")
    await tap(dp, bot, next(d for t, d in buttons(last("edit")[3]) if "Gaana" in t))
    await tap(dp, bot, next(d for t, d in buttons(last("edit")[3]) if "Buy Now" in t))
    await tap(dp, bot, next(d for t, d in buttons(last("edit")[3]) if "Confirm Purchase" in t))
    assert "demo-content-123" in " ".join(x[2] for x in LOG)
    print("OK instant purchase delivers the item in chat")
    posts = [x[2] for x in LOG if x[0] == "send" and x[1] == -1001234567890]
    assert len(posts) == 2, posts  # the invite order + the instant order
    last_post = posts[-1]
    assert "New Purchase" in last_post and "Gaana Plus" in last_post and "₹40" in last_post and "V***h" in last_post
    assert "demo-content-123" not in last_post and "Vikash" not in last_post and "cost" not in last_post.lower()
    assert "YouTube Premium" in posts[0] and "₹99" in posts[0]
    print("OK sales channel gets product + price + masked buyer, never cost or delivered items")

    await tap(dp, bot, "menu:orders")
    assert "Gaana" in last("edit")[2] and "YouTube" in last("edit")[2]
    await say(dp, bot, "/admin")
    assert "Admin Panel" in last("send")[2]
    for cb in ("adm:pay", "adm:man", "adm:chk", "adm:stats", "adm:prod"):
        await tap(dp, bot, cb)
    print("OK My Orders and every admin panel screen opens")

    await tap(dp, bot, "menu:addbal")
    await tap(dp, bot, "amt:500")
    assert last("send")[0] == "send" and "Pay ₹500" in last("send")[2]
    await tap(dp, bot, "paid")
    await say(dp, bot, "498765432109")
    assert "received" in last("send")[2]
    await say(dp, bot, "Rs.500.00 credited to A/c XX1234 by UPI Ref No 498765432109")  # admin pastes the bank SMS
    allt = " ".join(x[2] for x in LOG)
    assert "verified automatically" in allt
    async with SessionLocal() as s:
        assert (await s.get(User, 5)).balance_paise == 100000 - 9900 - 4000 + 50000
    print("OK UPI top-up: UTR submitted, bank SMS pasted by admin -> auto-approved")

    from app.supplier.base import SupplierClient, SupplierProduct

    class Fake(SupplierClient):
        async def list_products(self):
            return [SupplierProduct("p9", "Zed Tool", 1.5, 3, {}, "desc"), SupplierProduct("p1", "Alpha Tool", 0.5, None)]

    await say(dp, bot, "/supplier_list")  # dummy supplier -> friendly message
    assert "not connected" in last("send")[2]
    admin.supplier_client = Fake()
    await say(dp, bot, "/supplier_list")
    doc = last("doc")
    assert "2 supplier products" in doc[2] and doc[3].splitlines()[1].startswith("p1 | Alpha Tool | $0.50")
    assert "p9 | Zed Tool | $1.50" in doc[3]
    print("OK /supplier_list sends the product list as a file")

    # ---------------- force join ----------------
    CUST = TgUser(id=77, is_bot=False, first_name="Rohan")
    joined = set()
    state = {"broken": False}

    async def fake_member(bot_, chat, user_id):
        if state["broken"]:
            return None
        return (chat, user_id) in joined

    gate._is_member = fake_member
    gate.clear_cache()
    n = len(LOG)
    await say(dp, bot, "/start", CUST)
    out = [x for x in LOG[n:] if x[0] == "send"]
    assert out and "Join our channel" in out[-1][2] and "Welcome" not in " ".join(x[2] for x in LOG[n:])
    urls = [b.url for row in out[-1][3].inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/mychan", "https://t.me/+abc"], urls
    assert ("✅ I've Joined", "joined") in buttons(out[-1][3])
    print("OK new user is stopped at the join screen (channel links + I've Joined button)")

    n = len(LOG)
    await tap(dp, bot, "menu:buy", CUST)
    await say(dp, bot, "/products", CUST)
    assert "AI Subscriptions" not in " ".join(x[2] for x in LOG[n:] if x[0] in ("send", "edit"))
    print("OK buttons and commands are blocked until the user joins")

    n = len(LOG)
    await tap(dp, bot, "joined", CUST)
    assert [x for x in LOG[n:] if x[0] == "answer" and "not joined" in x[2] and x[3]] and "Welcome" not in " ".join(x[2] for x in LOG[n:])
    joined.add(("@mychan", 77))  # joined only one of the two
    n = len(LOG)
    await say(dp, bot, "/start", CUST)
    out = [x for x in LOG[n:] if x[0] == "send"][-1]
    urls = [b.url for row in out[3].inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/+abc"], urls
    print("OK 'I've Joined' without joining is refused; only the missing channel is shown")

    joined.add((-1001111, 77))
    n = len(LOG)
    await tap(dp, bot, "joined", CUST)
    assert "Welcome" in " ".join(x[2] for x in LOG[n:])
    n = len(LOG)
    await tap(dp, bot, "menu:buy", CUST)
    assert "AI Subscriptions" in last("edit")[2]
    print("OK after joining, 'I've Joined' opens the shop and everything works")

    gate.clear_cache()
    state["broken"] = True  # bot not admin / wrong channel: customers must NOT be locked out
    n = len(LOG)
    await say(dp, bot, "/start", CUST)
    assert "Welcome" in " ".join(x[2] for x in LOG[n:]) and any("cannot check" in x[2] for x in LOG[n:] if x[0] == "send")
    print("OK misconfigured channel -> customers not locked out, admin gets an alert")
    print("ALL BOT FLOW TESTS PASSED")


asyncio.run(main())
