import html
import math
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Plan, Product
from app.services import settings as settings_service
from app.services.catalog import CATALOG, _norm
from app.supplier import supplier

# (category name, emoji, keywords matched inside the supplier product title)
BRANDS = [
    ("ChatGPT", "🤖", ["chatgpt", "openai"]), ("Claude", "🧠", ["claude", "anthropic"]),
    ("Gemini", "✨", ["gemini"]), ("Perplexity", "🔎", ["perplexity"]), ("Canva", "🎨", ["canva"]),
    ("Netflix", "🎬", ["netflix"]), ("YouTube", "▶️", ["youtube"]), ("NordVPN", "🛡️", ["nordvpn"]),
    ("Notion", "📝", ["notion"]), ("LinkedIn", "💼", ["linkedin"]), ("Microsoft 365", "🪟", ["microsoft"]),
    ("Hotstar", "📺", ["hotstar"]), ("ElevenLabs", "🎙️", ["elevenlabs"]), ("HeyGen", "🎥", ["heygen"]),
    ("Midjourney", "🖼️", ["midjourney"]), ("Apple Music", "🎵", ["applemusic"]), ("Gaana", "🎶", ["gaana"]),
    ("Spotify", "🎧", ["spotify"]),
]
FALLBACK = ("Other Tools", "📦")


class SyncError(Exception):
    pass


def pick_category(title: str) -> tuple[str, str]:
    flat = title.lower().replace(" ", "").replace("-", "")
    for name, emoji, keys in BRANDS:
        if any(k in flat for k in keys):
            return name, emoji
    return FALLBACK


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    unavailable: int = 0
    demo_hidden: int = 0
    linked: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    loss: list = field(default_factory=list)

    def text(self) -> str:
        lines = ["🔄 <b>Sync finished</b>", "", f"➕ New plans: <b>{self.added}</b>", f"♻️ Updated: <b>{self.updated}</b>"]
        if self.linked:
            lines.append(f"🔗 Linked to supplier: <b>{len(self.linked)}</b>")
        if self.ambiguous:
            lines.append("\n❓ <b>Several supplier products match (not linked, pick one manually):</b>")
            lines += [f"• {html.escape(n)}" for n in self.ambiguous]
        if self.unmatched:
            lines.append("\n🚫 <b>Not found at supplier (not sellable until linked):</b>")
            lines += [f"• {html.escape(n)}" for n in self.unmatched]
        if self.unavailable:
            lines.append(f"🚫 No longer at supplier (stock set to 0): <b>{self.unavailable}</b>")
        if self.demo_hidden:
            lines.append(f"🧹 Demo plans hidden: <b>{self.demo_hidden}</b>")
        if self.loss:
            lines.append("\n⚠️ <b>Selling price is BELOW cost for:</b>")
            lines += [f"• {html.escape(n)}" for n in self.loss[:10]]
            lines.append("Fix these in Products & Plans before selling.")
        lines.append("\nYour selling prices are never overwritten. Use /import_all to also import every other supplier product.")
        return "\n".join(lines)


async def sync_products(import_new: bool = False) -> SyncReport:
    rate_s = await settings_service.get("usd_inr")
    if not rate_s:
        raise SyncError("Set the USD→INR rate first. Send: /rate 90   (1 USD = ₹90)")
    rate = float(rate_s)
    markup = float(await settings_service.get("markup_pct", "30"))

    items = await supplier.list_products()
    if not items:
        raise SyncError("The supplier returned no products.")

    rep = SyncReport()
    seen = set()
    async with SessionLocal() as s, s.begin():
        all_plans = (await s.execute(select(Plan))).scalars().all()
        plans = {p.supplier_ref: p for p in all_plans if p.supplier_ref}
        cats = {p.name: p for p in (await s.execute(select(Product))).scalars()}

        # 1) link catalog plans that have no supplier id yet, by matching words in the supplier's title
        for plan in all_plans:
            if plan.supplier_ref:
                continue
            entry = next((c for c in CATALOG if c["name"] == plan.name), None)
            if not entry:
                continue
            hits = [it for it in items if all(w in _norm(it.title) for w in entry["match"]) and it.ref not in plans]
            if len(hits) == 1:
                plan.supplier_ref = hits[0].ref
                plans[hits[0].ref] = plan
                rep.linked.append(plan.name)
            elif len(hits) > 1:
                rep.ambiguous.append(f"{plan.name}: " + " / ".join(h.title for h in hits[:4]))
            else:
                rep.unmatched.append(plan.name)

        # 2) update costs/stock of linked plans (and optionally import everything else)
        for it in items:
            seen.add(it.ref)
            cost = round(it.usd_price * rate * 100)
            plan = plans.get(it.ref)
            if plan:
                plan.cost_paise = cost
                if it.stock is not None:
                    plan.stock = it.stock
                elif plan.stock == 0:
                    plan.stock = None
                if not plan.description and it.description:
                    plan.description = it.description[:3000]
                rep.updated += 1
            elif import_new:
                cname, emoji = pick_category(it.title)
                cat = cats.get(cname)
                if not cat:
                    cat = Product(name=cname, emoji=emoji)
                    s.add(cat)
                    await s.flush()
                    cats[cname] = cat
                sell = math.ceil(cost * (1 + markup / 100) / 100) * 100
                # Titles with "invite" need the customer's email, which the supplier API cannot take -> manual flow
                kind = "invite" if "invite" in it.title.lower() else "instant"
                plan = Plan(product_id=cat.id, name=it.title[:128], sell_price_paise=sell, cost_paise=cost,
                            stock=it.stock, supplier_ref=it.ref, description=it.description[:3000], delivery=kind)
                s.add(plan)
                rep.added += 1
            else:
                continue
            if plan.sell_price_paise < plan.cost_paise:
                rep.loss.append(plan.name)
        for ref, plan in plans.items():
            if ref not in seen and plan.stock != 0:
                plan.stock = 0
                rep.unavailable += 1
        demo = (await s.execute(select(Plan).where(Plan.supplier_ref.is_(None), Plan.active))).scalars().all()
        for p in demo:
            if p.terms.startswith("DEMO plan"):
                p.active = False
                rep.demo_hidden += 1
    return rep
