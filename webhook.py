"""Tiny web server that receives bank SMS from an SMS-forwarder app on your phone.
Security: secret header (constant-time compare), bank sender allow-list, exact UTR+amount matching,
single-use credits, and an amount cap for auto-approval. Anything unclear is left for manual admin approval."""
import hmac
import json
import logging

from aiohttp import web

from app import config
from app.services import payments

log = logging.getLogger("webhook")
TEXT_KEYS = ("message", "text", "body", "sms", "content")
SENDER_KEYS = ("sender", "from", "address", "number", "originating_address")


def _pick(data: dict, keys) -> str:
    for k in keys:
        if data.get(k):
            return str(data[k])
    return ""


async def notify_result(bot, out: dict) -> None:
    """Tell the customer / admins what happened after a credit arrived."""
    from app.bot.handlers import alert_admins, inr

    res = out.get("result")
    if res == "approved":
        try:
            await bot.send_message(out["user_id"], f"✅ Payment verified automatically! <b>{inr(out['amount_paise'])}</b> added to your wallet.\nNew balance: <b>{inr(out['balance'])}</b>", parse_mode="HTML")
        except Exception:
            pass
        await alert_admins(bot, f"✅ Auto-approved payment #{out['id']} • {inr(out['amount_paise'])} • UTR <code>{out['utr']}</code>")
    elif res == "mismatch":
        await alert_admins(bot, f"⚠️ UTR <code>{out['utr']}</code> received, but the amount differs: customer claimed {inr(out['amount_paise'])}, bank shows {inr(out['credit_paise'])}. Decide in Pending Payments.")
    elif res == "too_large":
        await alert_admins(bot, f"ℹ️ Payment #{out['id']} ({inr(out['amount_paise'])}) is verified at the bank but above the auto-approve limit. Please approve it in Pending Payments.")


def build_app(bot) -> web.Application:
    async def sms(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Webhook-Secret", ""), config.WEBHOOK_SECRET):
            return web.json_response({"ok": False}, status=401)
        try:
            if "json" in request.content_type:
                data = await request.json()
            else:
                data = dict(await request.post())
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"ok": False, "error": "bad body"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "bad body"}, status=400)
        text, sender = _pick(data, TEXT_KEYS), _pick(data, SENDER_KEYS).lower()
        if config.BANK_SMS_SENDERS and not any(b in sender for b in config.BANK_SMS_SENDERS):
            log.warning("ignored SMS from unexpected sender %r", sender)
            return web.json_response({"ok": False, "error": "sender not allowed"}, status=403)
        if not config.BANK_SMS_SENDERS:
            return web.json_response({"ok": False, "error": "BANK_SMS_SENDERS not configured"}, status=403)
        out = await payments.ingest_credit_text(text, "webhook")
        await notify_result(bot, out)
        return web.json_response({"ok": True, "result": out.get("result")})

    async def health(_):
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=64 * 1024)
    app.add_routes([web.post("/webhook/sms", sms), web.get("/health", health)])
    return app


async def start(bot) -> web.AppRunner | None:
    if not config.WEBHOOK_SECRET:
        return None
    if len(config.WEBHOOK_SECRET) < 20:
        raise SystemExit("WEBHOOK_SECRET must be at least 20 characters.")
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    await web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT).start()
    print(f"Payment webhook listening on {config.WEBHOOK_HOST}:{config.WEBHOOK_PORT}")
    return runner
