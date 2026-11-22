import os
from dotenv import load_dotenv

load_dotenv()


def _ids(value: str) -> set[int]:
    return {int(x) for x in value.replace(" ", "").split(",") if x.strip().isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
UPI_ID = os.getenv("UPI_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./creative_agency.db")
BRAND = "Creative Agency"

# Supplier (Silvex AI reseller API). The key lives ONLY in .env, never in code.
SILVEX_API_KEY = os.getenv("SILVEX_API_KEY", "").strip()
SILVEX_BASE_URL = os.getenv("SILVEX_BASE_URL", "https://silvex-ai.com/api/public/reseller").rstrip("/")

# Payment auto-verification (bank SMS -> webhook). Leave WEBHOOK_SECRET empty to disable the web server.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080") or 8080)
BANK_SMS_SENDERS = [x.strip().lower() for x in os.getenv("BANK_SMS_SENDERS", "").split(",") if x.strip()]
AUTO_APPROVE_MAX_RS = int(os.getenv("AUTO_APPROVE_MAX_RS", "2000") or 2000)

# Public "sales feed" channel: a post is sent here after every purchase. @channelusername or numeric id (-100...). Empty = off.
_sc = os.getenv("SALES_CHANNEL", "").strip()
SALES_CHANNEL = (int(_sc) if _sc.lstrip("-").isdigit() else (_sc if _sc.startswith("@") or not _sc else "@" + _sc))


# Force-join: users must join these channel(s) before they can use the bot. Admins are exempt.
# Examples: FORCE_JOIN_CHANNELS=@mychannel            (public)
#           FORCE_JOIN_CHANNELS=-1001234567890|https://t.me/+AbCdEf   (private: id|invite link)
#           several: separate with commas
def _parse_channels(raw: str) -> list[tuple]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        chat, link = (x.strip() for x in part.split("|", 1)) if "|" in part else (part, None)
        chat = int(chat) if chat.lstrip("-").isdigit() else (chat if chat.startswith("@") else "@" + chat)
        if link is None and isinstance(chat, str):
            link = "https://t.me/" + chat[1:]
        out.append((chat, link))
    return out


FORCE_JOIN = _parse_channels(os.getenv("FORCE_JOIN_CHANNELS", ""))
