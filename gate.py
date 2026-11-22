"""Force-join check. A user is allowed when they are a member of EVERY configured channel."""
import logging
import time

from app import config

log = logging.getLogger("gate")
CACHE_SECONDS = 600
_ok_until: dict[int, float] = {}
_alerted: set = set()


def clear_cache() -> None:
    _ok_until.clear()
    _alerted.clear()


async def _is_member(bot, chat, user_id: int) -> bool | None:
    """True/False, or None when Telegram could not tell us (bot not admin, wrong channel...)."""
    try:
        m = await bot.get_chat_member(chat, user_id)
    except Exception as e:
        log.warning("get_chat_member failed for %s: %s", chat, e)
        return None
    status = getattr(m, "status", None)
    if status in ("member", "administrator", "creator"):
        return True
    if status == "restricted":
        return bool(getattr(m, "is_member", False))
    return False


async def missing_channels(bot, user_id: int, use_cache: bool = True) -> tuple[list, list]:
    """Returns (channels the user still has to join, channels we could not check)."""
    if not config.FORCE_JOIN:
        return [], []
    if use_cache and _ok_until.get(user_id, 0) > time.monotonic():
        return [], []
    missing, unknown = [], []
    for chat, link in config.FORCE_JOIN:
        ok = await _is_member(bot, chat, user_id)
        if ok is None:
            unknown.append(chat)
        elif not ok:
            missing.append((chat, link))
    if not missing:
        _ok_until[user_id] = time.monotonic() + CACHE_SECONDS
    return missing, unknown


def first_alert(chat) -> bool:
    """True only the first time a channel problem is seen (so admins are not spammed)."""
    if chat in _alerted:
        return False
    _alerted.add(chat)
    return True


async def startup_check(bot) -> None:
    if not config.FORCE_JOIN:
        return
    me = await bot.me()
    for chat, link in config.FORCE_JOIN:
        try:
            m = await bot.get_chat_member(chat, me.id)
            ok = m.status in ("administrator", "creator")
        except Exception:
            ok = False
        print(f"Force join {chat}: " + ("OK (bot is admin)" if ok else "WARNING - add the bot as ADMIN of this channel, otherwise users are NOT checked"))
        if link is None:
            print(f"  WARNING - private channel {chat} needs an invite link: {chat}|https://t.me/+xxxx")
