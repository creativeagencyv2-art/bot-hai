"""Run: python -m tests.test_catalog_autopay   (temporary database, local fake servers only)
NOTE: the sample bank SMS texts below are EXAMPLES. Real bank formats differ - test with your own bank's SMS."""
import asyncio
import os
import sqlite3
import tempfile

tmp = tempfile.mkdtemp()
DB = f"{tmp}/test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB}"
os.environ["WEBHOOK_SECRET"] = "x" * 24
os.environ["BANK_SMS_SENDERS"] = "HDFCBK,SBIUPI"
os.environ["AUTO_APPROVE_MAX_RS"] = "2000"

# an OLD database (before description/delivery/customer_info columns existed)
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE products (id INTEGER PRIMARY KEY, name VARCHAR(64), emoji VARCHAR(8), active BOOLEAN);
CREATE TABLE plans (id INTEGER PRIMARY KEY, product_id INTEGER, name VARCHAR(128), validity VARCHAR(64), activation VARCHAR(128),
  terms TEXT, sell_price_paise INTEGER, cost_paise INTEGER, stock INTEGER, supplier_ref VARCHAR(128), active BOOLEAN);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id BIGINT, plan_id INTEGER, idempotency_key VARCHAR(128) UNIQUE, status VARCHAR(24),
  sell_price_paise INTEGER, cost_paise INTEGER, supplier_order_id VARCHAR(128), note TEXT, created_at DATETIME);
INSERT INTO products VALUES (1,'ChatGPT','x',1);
INSERT INTO plans VALUES (1,1,'old demo',  '', '', 'DEMO plan - test data only.', 100, 50, NULL, NULL, 1);
""")
con.commit(); con.close()

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import webhook  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import BankCredit, Order, Payment, Plan, User  # noqa: E402
from app.services import catalog, orders, payments, settings, sync, wallet  # noqa: E402
from app.supplier.base import SupplierProduct, SupplierClient  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeSupplier(SupplierClient):
    requires_ref = True

    async def list_products(self):
        return [
            SupplierProduct("h1", "Hotstar Premium 1M (No Ads)", 0.30, None),
            SupplierProduct("n1", "Netflix Premium 1M – Shared Profile (4k UHD)", 1.20, 6),
            SupplierProduct("g6", "Gemini Plus 6M Links (No Card Required)", 0.75, 47),
            SupplierProduct("g12", "Gemini Plus 12M Links", 1.22, 0),
            SupplierProduct("a5", "Apple Music 5M", 0.25, 73),
            SupplierProduct("ga", "Gaana Plus 3M link", 0.05, 2382),
            SupplierProduct("y1", "YouTube 1 Month (Invite)", 0.50, None),
            SupplierProduct("c1", "Slot - Canva Edu Pro (Invite)", 0.30, None),
            SupplierProduct("x1", "YouTube Lite 3 Months A", 1.0, None),
            SupplierProduct("x2", "YouTube Lite 3 Months B", 1.0, None),
            SupplierProduct("z1", "Some Other Tool", 9.0, 5, description="Other tool details"),
            SupplierProduct("z2", "Notion Business 3M Link", 0.5, 27, description="Notion workspace link"),
            SupplierProduct("z3", "Spotify 1M (Invite)", 1.0, None),
        ]


async def bal(uid):
    async with SessionLocal() as s:
        return (await s.get(User, uid)).balance_paise


async def new_payment(uid, rupees, utr):
    return await payments.create_payment(uid, rupees * 100, utr)


async def main():
    await init_db()  # must migrate the old DB without errors
    await catalog.seed_catalog()
    await catalog.seed_catalog()  # idempotent
    async with SessionLocal() as s:
        plans = (await s.execute(select(Plan))).scalars().all()
        real = [p for p in plans if not p.terms.startswith("DEMO")]
        assert len(real) == 9, len(real)
        prices = {p.name: p.sell_price_paise // 100 for p in real}
        assert prices["Canva Pro (Invite)"] == 100 and prices["Netflix Premium – 1 Month (Shared Profile, 4K UHD)"] == 149
        assert prices["Gaana Plus – 3 Months (Link)"] == 40 and prices["Gemini Plus – 12 Months (Link)"] == 199
        assert sorted(prices.values()) == [40, 80, 99, 100, 120, 149, 149, 199, 199]
        assert not [p for p in plans if p.terms.startswith("DEMO") and p.active]
    print("OK old DB migrated; 9 catalog plans with your prices; demo plan hidden")

    # ---- sync links catalog plans by title ----
    await settings.set("usd_inr", "90")
    sync.supplier = FakeSupplier()
    rep = await sync.sync_products()
    assert len(rep.linked) == 8, rep.linked
    assert len(rep.ambiguous) == 1 and "Lite" in rep.ambiguous[0]
    assert rep.added == 0  # "Some Other Tool" is NOT imported
    async with SessionLocal() as s:
        hot = (await s.execute(select(Plan).where(Plan.supplier_ref == "h1"))).scalar_one()
        assert hot.cost_paise == 2700 and hot.sell_price_paise == 12000
        g12 = (await s.execute(select(Plan).where(Plan.supplier_ref == "g12"))).scalar_one()
        assert g12.stock == 0
        assert not (await s.execute(select(Plan).where(Plan.name == "Some Other Tool"))).first()
    rep3 = await sync.sync_products(import_new=True)
    async with SessionLocal() as s:
        notion = (await s.execute(select(Plan).where(Plan.supplier_ref == "z2"))).scalar_one()
        spot = (await s.execute(select(Plan).where(Plan.supplier_ref == "z3"))).scalar_one()
        other = (await s.execute(select(Plan).where(Plan.supplier_ref == "z1"))).scalar_one()
        assert notion.description == "Notion workspace link" and notion.delivery == "instant" and notion.sell_price_paise == 5900
        assert spot.delivery == "invite" and other.description == "Other tool details"
        hot = (await s.execute(select(Plan).where(Plan.supplier_ref == "h1"))).scalar_one()
        assert hot.sell_price_paise == 12000  # your own price untouched
        assert rep3.added == 5  # z1, z2, z3 + the two ambiguous YouTube Lite products (not guessed earlier)
    print("OK sync links by title, sets cost, respects stock, ambiguous match not guessed, no flooding")

    # ---- invite (manual) order ----
    async with SessionLocal() as s, s.begin():
        s.add(User(id=5, full_name="u"))
        await wallet.credit(s, 5, 100000, "admin_adjust", "t")
        yt = (await s.execute(select(Plan).where(Plan.name == "YouTube Premium – 1 Month (Invite)"))).scalar_one()
    orders.supplier = FakeSupplier()
    try:
        await orders.place_order(5, yt.id, "m0")
        raise AssertionError
    except orders.OrderError:
        pass
    assert await bal(5) == 100000
    out, oid = await orders.place_order(5, yt.id, "m1", "me@gmail.com")
    assert out == "manual" and await bal(5) == 100000 - 9900
    async with SessionLocal() as s:
        o = await s.get(Order, oid)
        assert o.status == "AWAITING_ADMIN" and o.customer_info == "me@gmail.com"
    assert (await orders.admin_mark_delivered(oid))["customer_info"] == "me@gmail.com"
    assert await orders.admin_mark_delivered(oid) is None
    out, oid2 = await orders.place_order(5, yt.id, "m2", "me@gmail.com")
    assert (await orders.admin_refund(oid2)) and await bal(5) == 100000 - 9900
    print("OK invite order: email required, manual status, delivered once, refund works")

    # ---- unlinked instant plan is blocked ----
    async with SessionLocal() as s, s.begin():
        lite_id = (await s.execute(select(Plan.id).where(Plan.name == "Netflix Premium – 1 Month (Shared Profile, 4K UHD)"))).scalar_one()
        (await s.get(Plan, lite_id)).supplier_ref = None
    try:
        await orders.place_order(5, lite_id, "u1")
        raise AssertionError
    except orders.OrderError:
        pass
    print("OK instant plan without supplier link is not sellable")

    # ---- auto payment verification ----
    async with SessionLocal() as s, s.begin():
        s.add(User(id=9, full_name="pay"))
        s.add(User(id=10, full_name="par"))
        s.add(User(id=11, full_name="hook"))

    # a) credit arrives AFTER the customer submitted the UTR
    p = await new_payment(9, 500, "411111111111")
    out = await payments.ingest_credit_text("Rs.500.00 credited to A/c XX1234 on 17-11-26 by UPI Ref No 411111111111. -HDFC", "t")
    assert out["result"] == "approved" and await bal(9) == 50000
    # b) same SMS again -> no double money
    out = await payments.ingest_credit_text("Rs.500.00 credited to A/c XX1234 by UPI Ref No 411111111111", "t")
    assert out["result"] == "duplicate" and await bal(9) == 50000
    print("OK credit after claim -> approved once, replay ignored")

    # c) credit arrives BEFORE the customer submits the UTR
    out = await payments.ingest_credit_text("Rs 300 credited to a/c XX1234. UPI Ref 422222222222", "t")
    assert out["result"] == "no_payment"
    p = await new_payment(9, 300, "422222222222")
    out = await payments.auto_match("422222222222")
    assert out["result"] == "approved" and await bal(9) == 80000
    print("OK credit before claim -> approved instantly")

    # d) wrong amount, e) too large, f) fake UTR, g) typed UTR alone
    await new_payment(9, 200, "433333333333")
    await payments.ingest_credit_text("Rs 150 credited UPI Ref 433333333333", "t")
    assert (await payments.auto_match("433333333333"))["result"] == "mismatch" and await bal(9) == 80000
    await new_payment(9, 5000, "444444444444")
    await payments.ingest_credit_text("Rs 5000 credited UPI Ref 444444444444", "t")
    assert (await payments.auto_match("444444444444"))["result"] == "too_large" and await bal(9) == 80000
    await new_payment(9, 100, "455555555555")
    assert (await payments.auto_match("455555555555"))["result"] == "no_credit" and await bal(9) == 80000
    print("OK mismatch / over-limit / no real credit -> NOT approved (left for admin)")

    # h) parallel matching -> money once
    await new_payment(10, 400, "466666666666")
    await payments.record_credit("466666666666", 40000, "t", "")
    res = await asyncio.gather(*[payments.auto_match("466666666666") for _ in range(6)])
    assert sum(1 for r in res if r["result"] == "approved") == 1 and await bal(10) == 40000
    print("OK 6 parallel matches -> credited once")

    # ---- webhook security + flow ----
    bot = FakeBot()
    await new_payment(11, 700, "477777777777")
    async with TestClient(TestServer(webhook.build_app(bot))) as cl:
        body = {"sender": "VM-HDFCBK", "message": "Rs.700 credited to A/c XX1 by UPI Ref No 477777777777"}
        r = await cl.post("/webhook/sms", json=body)
        assert r.status == 401
        r = await cl.post("/webhook/sms", json=body, headers={"X-Webhook-Secret": "wrong" * 5})
        assert r.status == 401
        r = await cl.post("/webhook/sms", json={**body, "sender": "AX-SPAMMR"}, headers={"X-Webhook-Secret": "x" * 24})
        assert r.status == 403 and await bal(11) == 0
        r = await cl.post("/webhook/sms", json=body, headers={"X-Webhook-Secret": "x" * 24})
        assert r.status == 200 and (await r.json())["result"] == "approved" and await bal(11) == 70000
        assert any(c == 11 and "verified automatically" in t for c, t in bot.sent)
    print("OK webhook: wrong secret 401, wrong sender 403, valid SMS auto-approves and notifies customer")
    print("ALL CATALOG + AUTOPAY TESTS PASSED")


asyncio.run(main())
