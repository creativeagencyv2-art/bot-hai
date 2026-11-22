from sqlalchemy import select

from app.db import SessionLocal
from app.models import Plan, Product


async def seed_demo() -> None:
    """Adds DEMO products once so you can test. Replace later from the admin panel."""
    async with SessionLocal() as s, s.begin():
        if (await s.execute(select(Product.id).limit(1))).first():
            return
        demo = {
            ("ChatGPT", "🤖"): [("ChatGPT Plus – 1 Month", "30 days", "Invite on your email", 150000, 100000)],
            ("Claude", "🧠"): [("Claude Pro – 1 Month", "30 days", "Invite on your email", 160000, 110000)],
            ("Gemini", "✨"): [("Gemini Advanced – 1 Month", "30 days", "Account link", 90000, 60000)],
            ("Perplexity", "🔎"): [("Perplexity Pro – 1 Year", "365 days", "Voucher code", 120000, 70000)],
        }
        for (name, emoji), plans in demo.items():
            p = Product(name=name, emoji=emoji)
            s.add(p)
            await s.flush()
            for pname, validity, act, sell, cost in plans:
                s.add(Plan(
                    product_id=p.id, name=pname, validity=validity, activation=act,
                    terms="DEMO plan – test data only. Replace before going live.",
                    sell_price_paise=sell, cost_paise=cost,
                ))


# ---------------------------------------------------------------------------
# Your live catalog. Selling prices are yours (₹). Cost is filled from the supplier on "Sync Products".
# "match" = words that must appear in the SUPPLIER's product title so the plan can be linked automatically.
# delivery: "instant" -> supplier delivers the link/account in the order response.
#           "invite"  -> customer's email is collected and your team sends the invite (admin marks delivered).
# ---------------------------------------------------------------------------
CATALOG = [
    {
        "category": ("Canva", "🎨"), "name": "Canva Pro (Invite)", "price": 100, "validity": "", "delivery": "invite",
        "match": ["canva"],
        "description": "Canva Pro features on your own Canva account, activated through a team invite.",
        "activation": "Send the email address of your Canva account at checkout. Our team sends the invite and you accept it from your inbox.",
        "terms": "Manual delivery by our team.\nUse the same email you use to log in to Canva.\nCheck your spam folder if you do not see the invite.",
    },
    {
        "category": ("Hotstar", "📺"), "name": "Hotstar Premium – 1 Month (No Ads)", "price": 120, "validity": "1 month", "delivery": "instant",
        "match": ["hotstar", "1m"],
        "description": "Movies, TV shows and live sports on Hotstar Premium. Available for Indian users only.",
        "activation": "1. Open the activation link we send you.\n2. Log in with your eligible Indian account.\n3. Choose UPI AutoPay / Mandate.\n4. Pick your UPI app and approve the mandate with your UPI PIN.\n5. Complete the activation as instructed.",
        "terms": "An Indian UPI account is required and your UPI app must support AutoPay.\nCheck the mandate amount and payment frequency before approving.\nCancel the AutoPay mandate if you do not want future automatic payments.",
    },
    {
        "category": ("Netflix", "🎬"), "name": "Netflix Premium – 1 Month (Shared Profile, 4K UHD)", "price": 149, "validity": "1 month", "delivery": "instant",
        "match": ["netflix", "1m"],
        "description": "Netflix Premium on a shared profile with 4K UHD streaming.",
        "activation": "Your login details are delivered here instantly after payment. Sign in and use your assigned profile.",
        "terms": "Shared profile access only.\nLogin is allowed on 1 device only.\nDo not change the account or profile settings.\nDo not change the password or email.\nDo not share the login details with anyone.",
    },
    {
        "category": ("YouTube", "▶️"), "name": "YouTube Premium – 1 Month (Invite)", "price": 99, "validity": "1 month", "delivery": "invite",
        "match": ["youtube", "1month", "invite"],
        "description": "YouTube Premium features on your own Google account, activated through an invite.",
        "activation": "Send a valid Google account email at checkout. Our team sends the invite manually and you accept it.",
        "terms": "Manual delivery by our team.\nIf you see country-related issues, turn off your payment profile before accepting the invite.\nIf you are already in a Google Family Group, leave it first and try again.",
    },
    {
        "category": ("Gemini", "✨"), "name": "Gemini Plus – 6 Months (Link, No Card Required)", "price": 149, "validity": "6 months", "delivery": "instant",
        "match": ["gemini", "6m"],
        "description": "6 months of Gemini Plus through a redeem link. No credit card required.\n• Premium Gemini AI access\n• 400 GB Google Drive, Photos and Gmail storage\n• Invite up to 5 accounts\n• 250 Google Flow points",
        "activation": "Your redeem link is delivered here instantly. Open it on your Google account and follow the steps.",
        "terms": "12-hour link hold warranty.\nLinks are bot-checked before delivery.",
    },
    {
        "category": ("Gemini", "✨"), "name": "Gemini Plus – 12 Months (Link)", "price": 199, "validity": "12 months", "delivery": "instant",
        "match": ["gemini", "12m"],
        "description": "12 months of Gemini Plus through an official Google redemption offer.\n• Premium Gemini AI access\n• 400 GB Google Cloud storage\n• Family sharing for up to 5 accounts\n• 250 Google Flow credits monthly",
        "activation": "1. Connect with an Indian proxy/VPN.\n2. Change the payment profile country of your Google account to India.\n3. Add a valid payment method and collect the offer.",
        "terms": "A valid payment method (credit or debit card) is required for activation.\nActivate on your personal Google account.",
    },
    {
        "category": ("Apple Music", "🎵"), "name": "Apple Music – 5 Months (Link)", "price": 80, "validity": "5 months", "delivery": "instant",
        "match": ["applemusic", "5m"],
        "description": "5 months of Apple Music through an activation link.",
        "activation": "Open the link we send you and create or use a fresh Indian-region Apple account. If you are outside India, use a VPN in the browser to activate.",
        "terms": "Works on a fresh account of the Indian region only.\nA valid UPI or card payment method is needed for activation (buyer's responsibility, we do not provide cards or UPI).\n1 month warranty after activation.\n24-hour link hold warranty.",
    },
    {
        "category": ("Gaana", "🎶"), "name": "Gaana Plus – 3 Months (Link)", "price": 40, "validity": "3 months", "delivery": "instant",
        "match": ["gaana", "3m"],
        "description": "3 months of Gaana Plus through an activation link.",
        "activation": "1. Install the Gaana app.\n2. Open the activation link we send you.\n3. Copy the redeem code shown on the redemption page.\n4. Paste it in the Redeem Code section of the app and submit.",
        "terms": "Instant delivery of the activation link.",
    },
    {
        "category": ("YouTube", "▶️"), "name": "YouTube Premium Lite – 3 Months (Invite)", "price": 199, "validity": "3 months", "delivery": "invite",
        "match": ["youtube", "lite"],
        "description": "YouTube Premium Lite on your own Google account, activated through an invite.",
        "activation": "Send a valid Google account email at checkout. Our team sends the invite manually and you accept it.",
        "terms": "Manual delivery by our team.\nIf you are already in a Google Family Group, leave it first and try again.",
    },
]


def _norm(text: str) -> str:
    return text.lower().replace(" ", "").replace("-", "")


async def seed_catalog() -> None:
    """Creates the catalog plans above (once, by plan name) and hides the old DEMO plans."""
    async with SessionLocal() as s, s.begin():
        cats = {p.name: p for p in (await s.execute(select(Product))).scalars()}
        names = {p.name for p in (await s.execute(select(Plan))).scalars()}
        for item in CATALOG:
            if item["name"] in names:
                continue
            cname, emoji = item["category"]
            cat = cats.get(cname)
            if not cat:
                cat = Product(name=cname, emoji=emoji)
                s.add(cat)
                await s.flush()
                cats[cname] = cat
            s.add(Plan(
                product_id=cat.id, name=item["name"], validity=item["validity"], activation=item["activation"],
                terms=item["terms"], description=item["description"], delivery=item["delivery"],
                sell_price_paise=item["price"] * 100, cost_paise=0,
            ))
        for p in (await s.execute(select(Plan).where(Plan.active))).scalars():
            if p.terms.startswith("DEMO plan"):
                p.active = False
