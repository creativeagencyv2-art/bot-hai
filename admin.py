import html
import json

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from sqlalchemy import func, select

from app import config
from app.bot.handlers import inr, show
from app.bot.keyboards import kb
from app.bot.pay_flow import pay_kb, payment_text
from app.db import SessionLocal
from app.models import Order, Plan, Product, User
from app.services import feed
from app.services import orders as order_service
from app.services import payments
from app.services import settings as settings_service
from app.services import sync as sync_service
from app.supplier import USING_REAL, supplier as supplier_client
from app.supplier.base import SupplierError, SupplierTimeout
from app import webhook as webhook_mod

router = Router()
# Everything in this file works ONLY for ADMIN_IDS from .env
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))


class AdminStates(StatesGroup):
    add_product = State()
    add_plan = State()
    edit_value = State()


def rupees_to_paise(text: str) -> int:
    return int(round(float(text.strip().replace("₹", "").replace(",", "")) * 100))


def back_admin(extra=None):
    rows = extra or []
    rows.append([Btn(text="⬅️ Admin Menu", callback_data="adm:home")])
    return kb(rows)


# ---------- Menu ----------
async def admin_menu(event):
    rows = [
        [Btn(text="🧾 Pending Payments", callback_data="adm:pay")],
        [Btn(text="📝 Manual Orders", callback_data="adm:man")],
        [Btn(text="📦 Products & Plans", callback_data="adm:prod")],
        [Btn(text="🔄 Sync Products", callback_data="adm:sync"), Btn(text="🏦 Supplier Balance", callback_data="adm:sbal")],
        [Btn(text="⚠️ Needs Check", callback_data="adm:chk"), Btn(text="📊 Stats", callback_data="adm:stats")],
    ]
    await show(event, "🛠️ <b>Admin Panel</b>", kb(rows))


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await admin_menu(m)


@router.callback_query(F.data == "adm:home")
async def cb_admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_menu(c)


# ---------- Payments ----------
@router.callback_query(F.data == "adm:pay")
async def pending(c: CallbackQuery):
    items = await payments.list_pending()
    if not items:
        return await show(c, "✅ No pending payments.", back_admin())
    await c.answer()
    for p, u in items:
        await c.message.answer(payment_text(p, u), reply_markup=pay_kb(p.id), parse_mode="HTML")
    await c.message.answer("☝️ These are all the pending payments.", reply_markup=back_admin())


@router.callback_query(F.data.startswith("pay:"))
async def pay_action(c: CallbackQuery):
    _, action, pid = c.data.split(":")
    ok = action == "ok"
    info = await (payments.approve if ok else payments.reject)(int(pid), c.from_user.id)
    if not info:
        return await c.answer("This payment was already processed.", show_alert=True)
    label = "✅ APPROVED" if ok else "❌ REJECTED"
    try:
        await c.message.edit_text(c.message.html_text + f"\n\n<b>{label}</b>", parse_mode="HTML")
    except Exception:
        pass
    try:
        if ok:
            text = f"✅ Payment approved! <b>{inr(info['amount_paise'])}</b> has been added to your wallet.\nNew balance: <b>{inr(info['balance'])}</b>"
        else:
            text = f"❌ Your payment request #{info['id']} could not be verified. If this is a mistake, contact support."
        await c.bot.send_message(info["user_id"], text, parse_mode="HTML")
    except Exception:
        pass
    await c.answer(label)


# ---------- Stats ----------
@router.callback_query(F.data == "adm:stats")
async def stats(c: CallbackQuery):
    async with SessionLocal() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar_one()
        rows = (await s.execute(
            select(Order.status, func.count(Order.id), func.coalesce(func.sum(Order.sell_price_paise), 0),
                   func.coalesce(func.sum(Order.sell_price_paise - Order.cost_paise), 0)).group_by(Order.status)
        )).all()
        pend = len(await payments.list_pending(100))
    by = {r[0]: r for r in rows}
    done = by.get("COMPLETED")
    lines = [f"{k}: {v[1]}" for k, v in by.items()] or ["No orders yet"]
    text = (
        "📊 <b>Stats</b>\n\n"
        f"👥 Customers: <b>{users}</b>\n🧾 Pending payments: <b>{pend}</b>\n\n"
        f"<b>Orders</b>\n" + "\n".join(lines) + "\n\n"
        f"💰 Revenue (completed): <b>{inr(done[2]) if done else '₹0'}</b>\n"
        f"📈 Profit (completed): <b>{inr(done[3]) if done else '₹0'}</b>"
    )
    await show(c, text, back_admin())


# ---------- Products ----------
@router.callback_query(F.data == "adm:prod")
async def products(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        items = (await s.execute(select(Product).order_by(Product.id))).scalars().all()
    rows = [[Btn(text=f"{'✅' if p.active else '🚫'} {p.emoji} {p.name}", callback_data=f"pr:{p.id}")] for p in items]
    rows.append([Btn(text="➕ Add Product", callback_data="adm:addprod")])
    await show(c, "📦 <b>Products</b>\nChoose a product to edit:", back_admin(rows))


@router.callback_query(F.data == "adm:addprod")
async def add_product(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_product)
    await c.message.answer("Send the new product in this format:\n<code>Name | emoji</code>\nExample: <code>Midjourney | 🎨</code>", parse_mode="HTML")
    await c.answer()


@router.message(StateFilter(AdminStates.add_product))
async def save_product(m: Message, state: FSMContext):
    parts = [x.strip() for x in (m.text or "").split("|")]
    if not parts[0]:
        return await m.answer("❌ Enter a name. Example: Midjourney | 🎨")
    async with SessionLocal() as s, s.begin():
        s.add(Product(name=parts[0][:64], emoji=(parts[1] if len(parts) > 1 and parts[1] else "🤖")[:8]))
    await state.clear()
    await m.answer("✅ Product added.", reply_markup=back_admin([[Btn(text="📦 Products", callback_data="adm:prod")]]))


async def product_page(event, pid: int):
    async with SessionLocal() as s:
        p = await s.get(Product, pid)
        plans = (await s.execute(select(Plan).where(Plan.product_id == pid).order_by(Plan.id))).scalars().all()
    rows = [[Btn(text=f"{'✅' if x.active else '🚫'} {x.name} | {inr(x.sell_price_paise)}", callback_data=f"pl:{x.id}")] for x in plans]
    rows.append([Btn(text="➕ Add Plan", callback_data=f"addplan:{pid}")])
    rows.append([Btn(text=("🚫 Hide product" if p.active else "✅ Show product"), callback_data=f"prtoggle:{pid}")])
    rows.append([Btn(text="⬅️ Products", callback_data="adm:prod")])
    await show(event, f"{p.emoji} <b>{p.name}</b>\nStatus: {'Visible ✅' if p.active else 'Hidden 🚫'}", kb(rows))


@router.callback_query(F.data.startswith("pr:"))
async def cb_product(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await product_page(c, int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("prtoggle:"))
async def toggle_product(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    async with SessionLocal() as s, s.begin():
        p = await s.get(Product, pid)
        p.active = not p.active
    await product_page(c, pid)


# ---------- Plans ----------
@router.callback_query(F.data.startswith("addplan:"))
async def add_plan(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_plan)
    await state.update_data(product_id=int(c.data.split(":")[1]))
    await c.message.answer(
        "Send the new plan in one message:\n"
        "<code>Name | Validity | Activation | Cost price | Selling price | Stock | Terms</code>\n\n"
        "Example:\n<code>ChatGPT Plus 1 Month | 30 days | Invite on email | 1000 | 1500 | 10 | No refund after activation</code>\n\n"
        "• Cost price = what the supplier charges you\n• Stock empty or <code>-</code> = unlimited\n• Terms are optional",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(StateFilter(AdminStates.add_plan))
async def save_plan(m: Message, state: FSMContext):
    parts = [x.strip() for x in (m.text or "").split("|")]
    try:
        assert len(parts) >= 5 and parts[0]
        cost, sell = rupees_to_paise(parts[3]), rupees_to_paise(parts[4])
        assert sell > 0 and cost >= 0
        stock = int(parts[5]) if len(parts) > 5 and parts[5] not in ("", "-") else None
        terms = parts[6] if len(parts) > 6 else ""
    except Exception:
        return await m.answer("❌ Wrong format. At least 5 fields are needed:\nName | Validity | Activation | Cost | Selling price")
    data = await state.get_data()
    async with SessionLocal() as s, s.begin():
        s.add(Plan(product_id=data["product_id"], name=parts[0], validity=parts[1], activation=parts[2],
                   cost_paise=cost, sell_price_paise=sell, stock=stock, terms=terms))
    await state.clear()
    await m.answer(f"✅ Plan added.\nProfit per sale: <b>{inr(sell - cost)}</b>", parse_mode="HTML",
                   reply_markup=back_admin([[Btn(text="⬅️ Product", callback_data=f"pr:{data['product_id']}")]]))


async def plan_page(event, plan_id: int):
    async with SessionLocal() as s:
        p = await s.get(Plan, plan_id)
    stock = "Unlimited" if p.stock is None else str(p.stock)
    text = (
        f"<b>{p.name}</b>\nStatus: {'Visible ✅' if p.active else 'Hidden 🚫'}\n\n"
        f"🏷️ Cost (supplier): <b>{inr(p.cost_paise)}</b>\n💵 Selling price: <b>{inr(p.sell_price_paise)}</b>\n"
        f"📈 Profit: <b>{inr(p.sell_price_paise - p.cost_paise)}</b>\n📦 Stock: {stock}\n"
        f"⏳ Validity: {p.validity or '-'}\n🔑 Activation: {p.activation or '-'}\n📜 Notes: {p.terms or '-'}\n"
        f"🔗 Supplier product ID: {p.supplier_ref or 'not linked'}"
    )
    rows = [
        [Btn(text="💵 Selling Price", callback_data=f"ed:sell:{p.id}"), Btn(text="🏷️ Cost", callback_data=f"ed:cost:{p.id}")],
        [Btn(text="📦 Stock", callback_data=f"ed:stock:{p.id}"), Btn(text="📜 Notes", callback_data=f"ed:terms:{p.id}")],
        [Btn(text="🔗 Supplier ID", callback_data=f"ed:ref:{p.id}"), Btn(text="📝 Description", callback_data=f"ed:desc:{p.id}")],
        [Btn(text="🔑 Activation", callback_data=f"ed:act:{p.id}"), Btn(text="📁 Category", callback_data=f"mv:{p.id}")],
        [Btn(text=("🚫 Hide plan" if p.active else "✅ Show plan"), callback_data=f"pltoggle:{p.id}")],
        [Btn(text="⬅️ Back", callback_data=f"pr:{p.product_id}")],
    ]
    await show(event, text, kb(rows))


@router.callback_query(F.data.startswith("pl:"))
async def cb_plan(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await plan_page(c, int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("pltoggle:"))
async def toggle_plan(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    async with SessionLocal() as s, s.begin():
        p = await s.get(Plan, pid)
        p.active = not p.active
    await plan_page(c, pid)


PROMPTS = {
    "sell": "Send the new <b>selling price</b> (₹). Example: 1500",
    "cost": "Send the new <b>cost price</b> (₹). Example: 1000",
    "stock": "Send the new <b>stock</b> number. Send <code>-</code> for unlimited.",
    "terms": "Send the new <b>notes</b> (one per line). Send <code>-</code> to clear.",
    "ref": "Send the supplier <b>product ID</b> for this plan. Send <code>-</code> to unlink.",
    "desc": "Send the new <b>description</b>. Send <code>-</code> to clear.",
    "act": "Send the new <b>activation instructions</b>. Send <code>-</code> to clear.",
}


@router.callback_query(F.data.startswith("ed:"))
async def edit_field(c: CallbackQuery, state: FSMContext):
    _, field, pid = c.data.split(":")
    await state.set_state(AdminStates.edit_value)
    await state.update_data(field=field, plan_id=int(pid))
    await c.message.answer(PROMPTS[field], parse_mode="HTML")
    await c.answer()


@router.message(StateFilter(AdminStates.edit_value))
async def save_edit(m: Message, state: FSMContext):
    data = await state.get_data()
    field, text = data["field"], (m.text or "").strip()
    try:
        async with SessionLocal() as s, s.begin():
            p = await s.get(Plan, data["plan_id"])
            if field == "sell":
                v = rupees_to_paise(text); assert v > 0; p.sell_price_paise = v
            elif field == "cost":
                v = rupees_to_paise(text); assert v >= 0; p.cost_paise = v
            elif field == "stock":
                p.stock = None if text == "-" else max(0, int(text))
            elif field == "terms":
                p.terms = "" if text == "-" else text
            elif field == "ref":
                p.supplier_ref = None if text == "-" else text[:128]
            elif field == "desc":
                p.description = "" if text == "-" else text
            elif field == "act":
                p.activation = "" if text == "-" else text
    except Exception:
        return await m.answer("❌ Send a valid value (numbers only for prices and stock).")
    await state.clear()
    await plan_page(m, data["plan_id"])


# ---------- Supplier tools ----------
@router.message(Command("rate"))
async def set_rate(m: Message):
    try:
        v = float(m.text.split()[1])
        assert 1 < v < 1000
    except Exception:
        return await m.answer("Use: /rate 90   (means 1 USD = ₹90)")
    await settings_service.set("usd_inr", str(v))
    await m.answer(f"✅ USD→INR rate set to ₹{v}. Run Sync Products to refresh costs.")


@router.message(Command("markup"))
async def set_markup(m: Message):
    try:
        v = float(m.text.split()[1])
        assert 0 <= v <= 500
    except Exception:
        return await m.answer("Use: /markup 30   (new plans get cost + 30%)")
    await settings_service.set("markup_pct", str(v))
    await m.answer(f"✅ Markup set to {v}% for NEW plans. Existing prices are not changed.")


@router.message(Command("api_check"))
async def api_check(m: Message):
    if not USING_REAL:
        return await m.answer("Supplier is not connected. Add SILVEX_API_KEY to .env and restart the bot.")
    lines = []
    try:
        bal, raw = await supplier_client.get_balance()
        lines.append(f"Balance: {bal if bal is not None else 'could not read'}  (fields: {sorted(raw)})")
    except (SupplierError, SupplierTimeout) as e:
        lines.append(f"Balance check failed: {html.escape(str(e))}")
    try:
        data = await supplier_client.fetch_products_raw()
        items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
        keys = sorted(items[0].keys()) if items and isinstance(items[0], dict) else "n/a"
        parsed = await supplier_client.list_products()
        lines.append(f"Products found: {len(items)}, readable: {len(parsed)}\nProduct fields: {keys}")
    except (SupplierError, SupplierTimeout) as e:
        lines.append(f"Products check failed: {html.escape(str(e))}")
    await m.answer("🔧 <b>Supplier check</b>\n\n" + "\n\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "adm:sync")
async def do_sync(c: CallbackQuery):
    await c.answer("Syncing…")
    try:
        rep = await sync_service.sync_products()
    except sync_service.SyncError as e:
        return await c.message.answer(f"❌ {html.escape(str(e))}", reply_markup=back_admin())
    except NotImplementedError as e:
        return await c.message.answer(f"❌ {html.escape(str(e))}", reply_markup=back_admin())
    except (SupplierError, SupplierTimeout) as e:
        return await c.message.answer(f"❌ Supplier error: {html.escape(str(e))}", reply_markup=back_admin())
    await c.message.answer(rep.text(), parse_mode="HTML", reply_markup=back_admin())


@router.callback_query(F.data == "adm:sbal")
async def supplier_balance(c: CallbackQuery):
    await c.answer()
    try:
        bal, raw = await supplier_client.get_balance()
    except (SupplierError, SupplierTimeout) as e:
        return await c.message.answer(f"❌ Supplier error: {html.escape(str(e))}", reply_markup=back_admin())
    if bal is None:
        return await c.message.answer("Could not read the balance from the response. Run /api_check.", reply_markup=back_admin())
    rate = await settings_service.get("usd_inr")
    inr_txt = f" (≈ ₹{bal * float(rate):,.0f})" if rate else ""
    await c.message.answer(f"🏦 <b>Supplier balance:</b> ${bal:,.2f}{inr_txt}", parse_mode="HTML", reply_markup=back_admin())


@router.callback_query(F.data == "adm:chk")
async def needs_check(c: CallbackQuery):
    from sqlalchemy.orm import selectinload

    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Order).options(selectinload(Order.plan)).where(Order.status == "NEEDS_CHECK").order_by(Order.id).limit(10)
        )).scalars().all()
    if not rows:
        return await show(c, "✅ No orders need checking.", back_admin())
    await c.answer()
    for o in rows:
        text = (f"⚠️ <b>Order #{o.id}</b>\n{html.escape(o.plan.name)}\nUser: <code>{o.user_id}</code>\n"
                f"Amount: <b>{inr(o.sell_price_paise)}</b>\nNote: {html.escape(o.note or '-')}\n\n"
                "Check the supplier's Orders page first. Refund only if the order was NOT delivered.")
        await c.message.answer(text, parse_mode="HTML", reply_markup=kb([[Btn(text="↩️ Refund customer", callback_data=f"chk:refund:{o.id}")]]))
    await c.message.answer("☝️ These orders need your decision.", reply_markup=back_admin())


@router.callback_query(F.data.startswith("chk:refund:"))
async def chk_refund(c: CallbackQuery):
    info = await order_service.admin_refund(int(c.data.split(":")[2]))
    if not info:
        return await c.answer("Already handled.", show_alert=True)
    try:
        await c.message.edit_text(c.message.html_text + "\n\n<b>↩️ REFUNDED</b>", parse_mode="HTML")
    except Exception:
        pass
    try:
        await c.bot.send_message(info["user_id"], f"↩️ Order #{info['id']} could not be completed. <b>{inr(info['amount_paise'])}</b> has been refunded to your wallet.", parse_mode="HTML")
    except Exception:
        pass
    await c.answer("Refunded")


@router.callback_query(F.data.startswith("mv:"))
async def move_plan(c: CallbackQuery):
    plan_id = int(c.data.split(":")[1])
    async with SessionLocal() as s:
        cats = (await s.execute(select(Product).order_by(Product.name))).scalars().all()
    rows = [[Btn(text=f"{p.emoji} {p.name}", callback_data=f"mvto:{plan_id}:{p.id}")] for p in cats]
    rows.append([Btn(text="⬅️ Back", callback_data=f"pl:{plan_id}")])
    await show(c, "Move this plan to which category?", kb(rows))


@router.callback_query(F.data.startswith("mvto:"))
async def move_plan_to(c: CallbackQuery):
    _, plan_id, prod_id = c.data.split(":")
    async with SessionLocal() as s, s.begin():
        (await s.get(Plan, int(plan_id))).product_id = int(prod_id)
    await plan_page(c, int(plan_id))


# ---------- Manual (invite) orders ----------
@router.callback_query(F.data == "adm:man")
async def manual_orders(c: CallbackQuery):
    from sqlalchemy.orm import selectinload

    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Order).options(selectinload(Order.plan)).where(Order.status == "AWAITING_ADMIN").order_by(Order.id).limit(10)
        )).scalars().all()
    if not rows:
        return await show(c, "✅ No manual orders waiting.", back_admin())
    await c.answer()
    for o in rows:
        text = (f"📝 <b>Manual order #{o.id}</b>\n{html.escape(o.plan.name)}\nCustomer: <code>{o.user_id}</code>\n"
                f"Email: <code>{html.escape(o.customer_info)}</code>\nPaid: <b>{inr(o.sell_price_paise)}</b>")
        await c.message.answer(text, parse_mode="HTML", reply_markup=kb([[Btn(text="✅ Delivered", callback_data=f"mo:done:{o.id}"), Btn(text="↩️ Refund", callback_data=f"mo:refund:{o.id}")]]))
    await c.message.answer("☝️ These orders are waiting for you.", reply_markup=back_admin())


@router.callback_query(F.data.startswith("mo:"))
async def manual_action(c: CallbackQuery):
    _, action, oid = c.data.split(":")
    done = action == "done"
    info = await (order_service.admin_mark_delivered if done else order_service.admin_refund)(int(oid))
    if not info:
        return await c.answer("Already handled.", show_alert=True)
    label = "✅ DELIVERED" if done else "↩️ REFUNDED"
    try:
        await c.message.edit_text(c.message.html_text + f"\n\n<b>{label}</b>", parse_mode="HTML")
    except Exception:
        pass
    try:
        if done:
            text = f"✅ <b>Order #{info['id']} is done!</b>\nThe invite has been sent to <code>{html.escape(info['customer_info'])}</code>. Please check your inbox (and spam folder) and accept it."
        else:
            text = f"↩️ Order #{info['id']} could not be completed. <b>{inr(info['amount_paise'])}</b> has been refunded to your wallet."
        await c.bot.send_message(info["user_id"], text, parse_mode="HTML")
    except Exception:
        pass
    await c.answer(label)


# ---------- Sales channel test ----------
@router.message(Command("channel_test"))
async def channel_test(m: Message):
    if not config.SALES_CHANNEL:
        return await m.answer("SALES_CHANNEL is empty in .env. Add your channel (@channelusername or -100... id) and restart the bot.")
    err = await feed.send_test(m.bot)
    if err is None:
        return await m.answer("✅ Test post sent. Check your channel.")
    await m.answer(f"❌ Could not post: {html.escape(err)}\n\nMake sure the bot is an <b>admin</b> of the channel with the <b>Post messages</b> permission, and that SALES_CHANNEL is correct.", parse_mode="HTML")


# ---------- Export the supplier's product list as a text file ----------
@router.message(Command("supplier_list"))
async def supplier_list(m: Message):
    try:
        items = await supplier_client.list_products()
    except NotImplementedError:
        return await m.answer("Supplier is not connected. Add SILVEX_API_KEY to .env and restart the bot.")
    except (SupplierError, SupplierTimeout) as e:
        return await m.answer(f"❌ Supplier error: {html.escape(str(e))}", parse_mode="HTML")
    rate = await settings_service.get("usd_inr")
    lines = ["ID | Title | USD price | INR cost | Stock | Has description"]
    for it in sorted(items, key=lambda x: x.title.lower()):
        inr_cost = f"₹{it.usd_price * float(rate):.2f}" if rate else "-"
        stock = "n/a" if it.stock is None else str(it.stock)
        lines.append(f"{it.ref} | {it.title} | ${it.usd_price:.2f} | {inr_cost} | {stock} | {'yes' if it.description else 'no'}")
    data = "\n".join(lines).encode("utf-8")
    await m.answer_document(BufferedInputFile(data, "supplier_products.txt"), caption=f"{len(items)} supplier products. No secrets in this file.")


# ---------- Import every supplier product (optional) ----------
@router.message(Command("import_all"))
async def import_all(m: Message):
    try:
        rep = await sync_service.sync_products(import_new=True)
    except (sync_service.SyncError, NotImplementedError) as e:
        return await m.answer(f"❌ {html.escape(str(e))}", parse_mode="HTML")
    except (SupplierError, SupplierTimeout) as e:
        return await m.answer(f"❌ Supplier error: {html.escape(str(e))}", parse_mode="HTML")
    await m.answer(rep.text(), parse_mode="HTML")


# ---------- Paste a bank credit SMS (works without any server) ----------
# IMPORTANT: keep this handler LAST in the file.
@router.message(StateFilter(None), F.text)
async def paste_bank_sms(m: Message):
    text = m.text or ""
    if text.startswith("/") or "credit" not in text.lower():
        return
    out = await payments.ingest_credit_text(text, "admin_paste")
    res = out.get("result")
    await webhook_mod.notify_result(m.bot, out)
    if res == "unreadable":
        return await m.answer("⚠️ I could not read an amount and a 12-digit UTR from this SMS. Approve manually in Pending Payments.")
    if res == "duplicate":
        return await m.answer("ℹ️ This UTR was already recorded.")
    if res == "approved":
        return await m.answer(f"✅ Credit recorded and payment #{out['id']} approved automatically.")
    if res in ("no_payment", "no_credit"):
        return await m.answer(f"✅ Credit of {inr(out['amount_paise_credit'])} recorded (UTR {out['utr']}). It will approve automatically when the customer submits this UTR.")
    await m.answer("✅ Credit recorded. It needs your decision (see the alert above).")
