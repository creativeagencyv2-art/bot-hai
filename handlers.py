import html
import math
import re
import uuid

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from app import config
from app.bot.keyboards import back_home, kb, main_menu
from app.db import SessionLocal
from app.models import Order, Plan, Product, User, WalletTransaction
from app.bot.force_join import gate_markup, gate_text
from app.services import deliveries, feed, gate
from app.services import orders as order_service
from app.services import wallet
from app.services.wallet import InsufficientFunds
from app.supplier import supplier as supplier_client

router = Router()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BuyStates(StatesGroup):
    email = State()


class SearchStates(StatesGroup):
    query = State()


def visible_plan():
    """Plans customers may see: active, and either hand-fulfilled (invite) or linked to a supplier product."""
    cond = [Plan.delivery == "invite", Plan.supplier_ref.is_not(None)]
    if not getattr(supplier_client, "requires_ref", False):
        cond.append(Plan.id.is_not(None))
    return and_(Plan.active, or_(*cond))


def inr(paise: int) -> str:
    return f"₹{paise // 100:,}" if paise % 100 == 0 else f"₹{paise / 100:,.2f}"


async def show(event: Message | CallbackQuery, text: str, markup) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest:
            pass
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


async def ensure_user(tg_user) -> User:
    async with SessionLocal() as s, s.begin():
        user = await s.get(User, tg_user.id)
        if not user:
            user = User(id=tg_user.id, username=tg_user.username, full_name=tg_user.full_name or "")
            s.add(user)
        else:
            user.username, user.full_name = tg_user.username, tg_user.full_name or ""
        return user


# ---------- Main menu ----------
async def home(event: Message | CallbackQuery):
    user = await ensure_user(event.from_user)
    text = f"Hey <b>{event.from_user.first_name}</b> 👋\nWelcome to <b>{config.BRAND}</b>!\n\n💰 Wallet: <b>{inr(user.balance_paise)}</b>\n\nWhat would you like to do?"
    await show(event, text, main_menu())


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await home(m)


@router.callback_query(F.data == "joined")
async def cb_joined(c: CallbackQuery, state: FSMContext):
    missing, _ = await gate.missing_channels(c.bot, c.from_user.id, use_cache=False)
    if missing and c.from_user.id not in config.ADMIN_IDS:
        return await c.answer("You have not joined yet. Please join the channel first.", show_alert=True)
    await state.clear()
    await home(c)


@router.callback_query(F.data == "menu:home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await home(c)


# ---------- Buy flow ----------
PAGE_SIZE = 10


def item_label(p: Plan, emoji: str) -> str:
    name = p.name if len(p.name) <= 38 else p.name[:37] + "…"
    stock = ""
    if p.stock is not None:
        stock = f" | 📦 {p.stock}" if p.stock > 0 else " | ❌ Out"
    return f"{emoji} {name} | {inr(p.sell_price_paise)}{stock}"


async def visible_items() -> list[tuple[Plan, str]]:
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Plan, Product.emoji).join(Product, Product.id == Plan.product_id)
            .where(Product.active, visible_plan()).order_by(Product.id, Plan.id)
        )).all()
    return [(p, e) for p, e in rows]


async def listing(event, page: int):
    rows = await visible_items()
    if not rows:
        return await show(event, "No products are available right now.", back_home())
    total_pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    buttons = [[Btn(text=item_label(p, e), callback_data=f"plan:{p.id}:{page}")] for p, e in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    nav = []
    if page > 0:
        nav.append(Btn(text="⬅️ Previous Page", callback_data=f"pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(Btn(text="➡️ Next Page", callback_data=f"pg:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Btn(text="🔍 Search products", callback_data="search")])
    buttons.append([Btn(text="🏠 Back to Shop", callback_data="menu:home")])
    await show(event, f"🛍️ <b>AI Subscriptions</b>\nPage {page + 1} of {total_pages} — tap any item below to see details.", kb(buttons))


@router.callback_query(F.data == "menu:buy")
async def buy(c: CallbackQuery):
    await listing(c, 0)


@router.callback_query(F.data.startswith("pg:"))
async def go_page(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await listing(c, int(c.data.split(":")[1]))


@router.callback_query(F.data == "search")
async def search_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.query)
    await c.message.answer("🔍 Type the product name to search (example: Netflix):")
    await c.answer()


@router.message(StateFilter(SearchStates.query), ~F.text.startswith("/"))
async def search_run(m: Message, state: FSMContext):
    q = (m.text or "").strip()
    flat = q.lower().replace(" ", "")
    if len(flat) < 2:
        return await m.answer("Please type at least 2 letters of the product name.")
    await state.clear()
    hits = [(p, e) for p, e in await visible_items() if flat in p.name.lower().replace(" ", "")][:PAGE_SIZE]
    again = [Btn(text="🔍 Search again", callback_data="search")]
    if not hits:
        return await m.answer(f"No products found for <b>{html.escape(q)}</b>.", parse_mode="HTML", reply_markup=kb([again, [Btn(text="⬅️ All products", callback_data="pg:0")]]))
    rows = [[Btn(text=item_label(p, e), callback_data=f"plan:{p.id}:0")] for p, e in hits]
    rows.append(again)
    rows.append([Btn(text="⬅️ All products", callback_data="pg:0"), Btn(text="🏠 Main Menu", callback_data="menu:home")])
    await m.answer(f"🔍 Results for <b>{html.escape(q)}</b>:", parse_mode="HTML", reply_markup=kb(rows))


def plan_text(p: Plan) -> str:
    e = html.escape
    delivery = "🕐 Manual — invite sent to your email by our team" if p.delivery == "invite" else "⚡ Instant — delivered here right after payment"
    lines = [f"<b>{e(p.name)}</b>", "", f"💵 Price: <b>{inr(p.sell_price_paise)}</b>"]
    if p.validity:
        lines.append(f"⏳ Validity: {e(p.validity)}")
    lines.append(f"🚚 Delivery: {delivery}")
    if p.stock is not None:
        lines.append("📦 Stock: " + (str(p.stock) if p.stock > 0 else "<b>Out of stock</b>"))
    if p.description:
        lines += ["", "📝 <b>About</b>", e(p.description)]
    if p.activation:
        lines += ["", "🔑 <b>How to activate</b>", e(p.activation)]
    notes = [t.strip() for t in (p.terms or "").splitlines() if t.strip()]
    if notes:
        lines += ["", "⚠️ <b>Important notes</b>"] + [f"▪️ {e(t)}" for t in notes]
    return "\n".join(lines)[:4000]


@router.callback_query(F.data.startswith("plan:"))
async def plan_detail(c: CallbackQuery):
    parts = c.data.split(":")
    plan_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with SessionLocal() as s:
        p = await s.get(Plan, plan_id)
    rows = []
    if p.stock is None or p.stock > 0:
        rows.append([Btn(text=f"✅ Buy Now — {inr(p.sell_price_paise)}", callback_data=f"ask:{p.id}")])
    rows.append([Btn(text="⬅️ Back to Products", callback_data=f"pg:{page}"), Btn(text="🏠 Main Menu", callback_data="menu:home")])
    await show(c, plan_text(p), kb(rows))


async def send_confirm(event, user: User, p: Plan, email: str = ""):
    token = uuid.uuid4().hex[:12]  # one token per confirm screen => double taps can't create 2 orders
    if p.stock is not None and p.stock <= 0:
        return await show(event, "❌ This plan is out of stock.", back_home())
    if user.balance_paise < p.sell_price_paise:
        short = p.sell_price_paise - user.balance_paise
        text = f"❌ Insufficient balance.\n\nPrice: <b>{inr(p.sell_price_paise)}</b>\nWallet: <b>{inr(user.balance_paise)}</b>\nYou need: <b>{inr(short)}</b>"
        rows = [[Btn(text="➕ Add Balance", callback_data="menu:addbal")], [Btn(text="⬅️ Back", callback_data=f"plan:{p.id}")]]
    else:
        extra = f"\nInvite email: <code>{html.escape(email)}</code>" if email else ""
        text = (
            f"🧾 <b>Confirm your order</b>\n\n{html.escape(p.name)}\nPrice: <b>{inr(p.sell_price_paise)}</b>{extra}\n"
            f"Wallet: {inr(user.balance_paise)} → <b>{inr(user.balance_paise - p.sell_price_paise)}</b>"
        )
        rows = [[Btn(text="✅ Confirm Purchase", callback_data=f"do:{p.id}:{token}")], [Btn(text="❌ Cancel", callback_data="menu:home")]]
    await show(event, text, kb(rows))


@router.callback_query(F.data.startswith("ask:"))
async def ask_confirm(c: CallbackQuery, state: FSMContext):
    plan_id = int(c.data.split(":")[1])
    user = await ensure_user(c.from_user)
    async with SessionLocal() as s:
        p = await s.get(Plan, plan_id)
    if p.delivery == "invite":
        data = await state.get_data()
        if data.get("email_plan") != plan_id or not data.get("email"):
            await state.set_state(BuyStates.email)
            await state.update_data(email_plan=plan_id, email=None)
            await c.message.answer("📧 Please send the <b>email address</b> where the invite should be sent:", parse_mode="HTML")
            return await c.answer()
        return await send_confirm(c, user, p, data["email"])
    await send_confirm(c, user, p)


@router.message(StateFilter(BuyStates.email))
async def got_email(m: Message, state: FSMContext):
    email = (m.text or "").strip()
    if not EMAIL_RE.match(email) or len(email) > 120:
        return await m.answer("❌ That does not look like a valid email. Please send it again (example: name@gmail.com).")
    data = await state.get_data()
    await state.set_state(None)
    await state.update_data(email=email)
    user = await ensure_user(m.from_user)
    async with SessionLocal() as s:
        p = await s.get(Plan, data["email_plan"])
    await send_confirm(m, user, p, email)


async def alert_admins(bot, text: str) -> None:
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(F.data.startswith("do:"))
async def do_purchase(c: CallbackQuery, state: FSMContext):
    _, plan_id, token = c.data.split(":")
    await ensure_user(c.from_user)
    data = await state.get_data()
    email = data.get("email") if data.get("email_plan") == int(plan_id) else ""
    try:
        outcome, order_id = await order_service.place_order(c.from_user.id, int(plan_id), f"{c.from_user.id}:{token}", email or "")
    except InsufficientFunds:
        return await show(c, "❌ Insufficient balance. Please add balance first.", kb([[Btn(text="➕ Add Balance", callback_data="menu:addbal")]]))
    except order_service.OrderError as e:
        return await show(c, f"❌ {e}", back_home())

    messages = {
        "ok": f"✅ <b>Order #{order_id} successful!</b>\nYour details are below. You can open them again anytime from My Orders.",
        "manual": f"🕐 <b>Order #{order_id} received!</b>\nOur team will send the invite to <code>{html.escape(email or '')}</code> shortly. You will get a message here as soon as it is done.",
        "processing": f"⏳ <b>Order #{order_id} is being processed.</b>\nYou will be notified here as soon as it is delivered.",
        "needs_check": f"⚠️ <b>Order #{order_id} is under review.</b>\nWe are verifying it. Your money is safe.",
        "refunded": "❌ Your order failed. The amount has been refunded to your wallet.",
        "duplicate": "ℹ️ This order was already placed. Check My Orders.",
    }
    await show(c, messages[outcome], kb([[Btn(text="📦 My Orders", callback_data="menu:orders")], [Btn(text="🏠 Main Menu", callback_data="menu:home")]]))
    if outcome == "ok" and order_id:
        for part in await deliveries.messages(order_id):
            await c.message.answer(part, parse_mode="HTML")
    if outcome == "manual" and order_id:
        await state.update_data(email=None, email_plan=None)
        async with SessionLocal() as s:
            o = (await s.execute(select(Order).options(selectinload(Order.plan)).where(Order.id == order_id))).scalar_one()
        for admin_id in config.ADMIN_IDS:
            try:
                await c.bot.send_message(
                    admin_id,
                    f"📝 <b>New manual order #{o.id}</b>\n{html.escape(o.plan.name)}\nCustomer: <code>{o.user_id}</code>\nEmail: <code>{html.escape(o.customer_info)}</code>\nPaid: <b>{inr(o.sell_price_paise)}</b>\n\nBuy it from your supplier with this email, then tap Delivered.",
                    parse_mode="HTML", reply_markup=kb([[Btn(text="✅ Delivered", callback_data=f"mo:done:{o.id}"), Btn(text="↩️ Refund", callback_data=f"mo:refund:{o.id}")]]),
                )
            except Exception:
                pass
    if outcome in ("ok", "processing", "manual") and order_id:
        await feed.post_sale(c.bot, order_id)
    if outcome in ("refunded", "needs_check") and order_id:
        async with SessionLocal() as s:
            o = await s.get(Order, order_id)
        await alert_admins(c.bot, f"⚠️ Order #{order_id} ({outcome}) for user <code>{c.from_user.id}</code>.\nReason: {html.escape(o.note or '-')}")


# ---------- Orders ----------
STATUS = {"COMPLETED": "✅ Completed", "PROCESSING": "⏳ Processing", "NEEDS_CHECK": "⚠️ Under review", "AWAITING_ADMIN": "🕐 Processing (manual)", "REFUNDED": "↩️ Refunded", "FUNDS_HELD": "⏳ Pending"}


@router.callback_query(F.data == "menu:orders")
async def my_orders(c: CallbackQuery):
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Order).options(selectinload(Order.plan)).where(Order.user_id == c.from_user.id).order_by(Order.id.desc()).limit(10)
        )).scalars().all()
    if not rows:
        return await show(c, "📦 You have no orders yet.", back_home())
    lines = [f"#{o.id} • {o.plan.name} • {inr(o.sell_price_paise)} • {STATUS.get(o.status, o.status)}" for o in rows]
    buttons = [[Btn(text=f"#{o.id} {o.plan.name[:28]}", callback_data=f"ord:{o.id}")] for o in rows]
    buttons.append([Btn(text="🏠 Main Menu", callback_data="menu:home")])
    await show(c, "📦 <b>Your recent orders</b>\nTap an order to see its details.\n\n" + "\n".join(lines), kb(buttons))


@router.callback_query(F.data.startswith("ord:"))
async def order_detail(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    async with SessionLocal() as s:
        o = (await s.execute(
            select(Order).options(selectinload(Order.plan)).where(Order.id == oid, Order.user_id == c.from_user.id)
        )).scalar_one_or_none()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    text = f"📦 <b>Order #{o.id}</b>\n{o.plan.name}\nPrice: <b>{inr(o.sell_price_paise)}</b>\nStatus: {STATUS.get(o.status, o.status)}\nDate: {o.created_at:%d %b %Y %H:%M}" + (f"\nInvite email: {html.escape(o.customer_info)}" if o.customer_info else "")
    await show(c, text, kb([[Btn(text="⬅️ My Orders", callback_data="menu:orders")], [Btn(text="🏠 Main Menu", callback_data="menu:home")]]))
    if o.status == "COMPLETED":
        for part in await deliveries.messages(o.id):
            await c.message.answer(part, parse_mode="HTML")


# ---------- Wallet ----------
@router.callback_query(F.data == "menu:wallet")
async def my_wallet(c: CallbackQuery):
    user = await ensure_user(c.from_user)
    async with SessionLocal() as s:
        txs = (await s.execute(
            select(WalletTransaction).where(WalletTransaction.user_id == c.from_user.id).order_by(WalletTransaction.id.desc()).limit(8)
        )).scalars().all()
    hist = "\n".join(f"{'➕' if t.amount_paise > 0 else '➖'} {inr(abs(t.amount_paise))} • {t.type} • {t.created_at:%d %b %H:%M}" for t in txs) or "No transactions yet."
    text = f"💰 <b>My Wallet</b>\n\nBalance: <b>{inr(user.balance_paise)}</b>\n\n<b>Recent transactions</b>\n{hist}"
    await show(c, text, kb([[Btn(text="➕ Add Balance", callback_data="menu:addbal")], [Btn(text="🏠 Main Menu", callback_data="menu:home")]]))


# ---------- Menu-button commands (/products /wallet /orders) ----------
@router.message(Command("products"))
async def cmd_products(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    await buy(m)


@router.message(Command("wallet"))
async def cmd_wallet(m: Message, state: FSMContext):
    await state.clear()
    await my_wallet(m)


@router.message(Command("orders"))
async def cmd_orders(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    await my_orders(m)


# ---------- Simple pages ----------
@router.callback_query(F.data == "menu:offers")
async def offers(c: CallbackQuery):
    await show(c, "🎁 <b>Offers</b>\n\nNo active offers right now. Check back soon! 🔥", back_home())


@router.callback_query(F.data == "menu:support")
async def support(c: CallbackQuery):
    await show(c, f"📞 <b>Customer Support</b>\n\nMessage us for help: @{config.SUPPORT_USERNAME}", back_home())


@router.callback_query(F.data == "menu:about")
async def about(c: CallbackQuery):
    await show(c, f"ℹ️ <b>{config.BRAND}</b>\n\nAI tool subscriptions, right inside Telegram. Fast, simple, secure.", back_home())


# ---------- Admin (temporary test command) ----------
@router.message(Command("credit"))
async def admin_credit(m: Message):
    """/credit <telegram_user_id> <rupees>  -- admin only, logged in wallet_transactions"""
    if m.from_user.id not in config.ADMIN_IDS:
        return
    try:
        _, uid, rupees = m.text.split()
        uid, paise = int(uid), int(round(float(rupees) * 100))
        assert paise > 0
    except Exception:
        return await m.answer("Use: /credit <user_id> <amount>\nExample: /credit 123456789 500")
    async with SessionLocal() as s, s.begin():
        if not await s.get(User, uid):
            return await m.answer("This user has not started the bot yet.")
        bal = await wallet.credit(s, uid, paise, "admin_adjust", f"Credited by admin {m.from_user.id}")
    await m.answer(f"✅ {inr(paise)} added. New balance: {inr(bal)}")
