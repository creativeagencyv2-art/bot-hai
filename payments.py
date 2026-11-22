import re
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app import config
from app.db import SessionLocal
from app.models import BankCredit, Payment, User
from app.services import wallet


class PaymentError(Exception):
    pass


async def create_payment(user_id: int, amount_paise: int, utr: str) -> Payment:
    async with SessionLocal() as s:
        if (await s.execute(select(Payment.id).where(Payment.utr == utr))).first():
            raise PaymentError("This UTR has already been used. Send the correct UTR or contact support.")
        pending = (await s.execute(
            select(func.count(Payment.id)).where(Payment.user_id == user_id, Payment.status == "PENDING")
        )).scalar_one()
        if pending >= 3:
            raise PaymentError("You already have 3 payments waiting for verification. Please wait.")
    try:
        async with SessionLocal() as s, s.begin():
            p = Payment(user_id=user_id, amount_paise=amount_paise, utr=utr)
            s.add(p)
            await s.flush()
            return p
    except IntegrityError:
        raise PaymentError("This UTR has already been used.")


async def list_pending(limit: int = 10) -> list[tuple[Payment, User]]:
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Payment, User).join(User, User.id == Payment.user_id)
            .where(Payment.status == "PENDING").order_by(Payment.id).limit(limit)
        )).all()
        return [(p, u) for p, u in rows]


async def approve(payment_id: int, admin_id: int) -> dict | None:
    """Adds money to the wallet exactly once. Returns None if already processed."""
    async with SessionLocal() as s, s.begin():
        p = await s.get(Payment, payment_id)
        if not p or p.status != "PENDING":
            return None
        info = {"user_id": p.user_id, "amount_paise": p.amount_paise, "utr": p.utr, "id": p.id}
        res = await s.execute(  # atomic: only one admin tap can win
            update(Payment).where(Payment.id == payment_id, Payment.status == "PENDING")
            .values(status="APPROVED", reviewed_by=admin_id, reviewed_at=datetime.utcnow())
        )
        if res.rowcount == 0:
            return None
        info["balance"] = await wallet.credit(
            s, p.user_id, p.amount_paise, "topup", f"Payment #{p.id} UTR {p.utr} (approved by {admin_id})"
        )
        return info


async def reject(payment_id: int, admin_id: int) -> dict | None:
    async with SessionLocal() as s, s.begin():
        p = await s.get(Payment, payment_id)
        if not p or p.status != "PENDING":
            return None
        info = {"user_id": p.user_id, "amount_paise": p.amount_paise, "utr": p.utr, "id": p.id}
        res = await s.execute(
            update(Payment).where(Payment.id == payment_id, Payment.status == "PENDING")
            .values(status="REJECTED", note=f"Rejected. UTR was {p.utr}", utr=None,
                    reviewed_by=admin_id, reviewed_at=datetime.utcnow())
        )
        return info if res.rowcount else None


# ---------------------------------------------------------------------------
# Automatic verification: a payment is approved ONLY when the same UTR AND the exact amount
# appear in a record of money that really arrived (bank SMS / gateway feed). A typed UTR alone is never enough.
# ---------------------------------------------------------------------------
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_UTR_LABELLED = re.compile(r"(?:upi\s*ref(?:erence)?(?:\s*(?:no|number|id))?|ref(?:erence)?\s*(?:no|number|id)?|utr|rrn|txn\s*id)\W{0,12}(\d{12})", re.I)
_UTR_ANY = re.compile(r"(?<!\d)(\d{12})(?!\d)")


def parse_credit_text(text: str) -> tuple[int, str] | None:
    """Reads a bank credit SMS. Returns (amount_paise, utr) or None if it is not clearly a credit."""
    low = text.lower()
    if "credit" not in low or "debit" in low:
        return None
    m = _AMOUNT.search(text)
    if not m:
        return None
    try:
        amount = round(float(m.group(1).replace(",", "")) * 100)
    except ValueError:
        return None
    u = _UTR_LABELLED.search(text) or _UTR_ANY.search(text)
    if not u or amount <= 0:
        return None
    return amount, u.group(1)


async def record_credit(utr: str, amount_paise: int, source: str, raw_text: str = "") -> bool:
    """Stores a real incoming credit. Returns False if this UTR was already recorded."""
    try:
        async with SessionLocal() as s, s.begin():
            if (await s.execute(select(BankCredit.id).where(BankCredit.utr == utr))).first():
                return False
            s.add(BankCredit(utr=utr, amount_paise=amount_paise, source=source, raw_text=raw_text[:500]))
        return True
    except IntegrityError:
        return False


async def auto_match(utr: str) -> dict:
    """Tries to approve the PENDING payment with this UTR against a recorded credit.
    Returns {"result": approved|no_credit|no_payment|mismatch|too_large, ...}."""
    class _Abort(Exception):
        pass

    try:
        async with SessionLocal() as s, s.begin():
            credit = (await s.execute(select(BankCredit).where(BankCredit.utr == utr, BankCredit.claimed_payment_id.is_(None)))).scalar_one_or_none()
            pay = (await s.execute(select(Payment).where(Payment.utr == utr, Payment.status == "PENDING"))).scalar_one_or_none()
            if not pay:
                return {"result": "no_payment"}
            if not credit:
                return {"result": "no_credit", "payment_id": pay.id}
            info = {"user_id": pay.user_id, "amount_paise": pay.amount_paise, "utr": utr, "id": pay.id, "payment_id": pay.id}
            if credit.amount_paise != pay.amount_paise:
                return {"result": "mismatch", "credit_paise": credit.amount_paise, **info}
            if pay.amount_paise > config.AUTO_APPROVE_MAX_RS * 100:
                return {"result": "too_large", **info}
            r1 = await s.execute(update(Payment).where(Payment.id == pay.id, Payment.status == "PENDING")
                                 .values(status="APPROVED", reviewed_by=0, reviewed_at=datetime.utcnow(), note="Auto-verified against bank credit"))
            r2 = await s.execute(update(BankCredit).where(BankCredit.id == credit.id, BankCredit.claimed_payment_id.is_(None))
                                 .values(claimed_payment_id=pay.id))
            if r1.rowcount == 0 or r2.rowcount == 0:
                raise _Abort()  # someone else got there first -> roll everything back
            info["balance"] = await wallet.credit(s, pay.user_id, pay.amount_paise, "topup", f"Payment #{pay.id} UTR {utr} (auto-verified)")
            return {"result": "approved", **info}
    except _Abort:
        return {"result": "no_payment"}


async def ingest_credit_text(text: str, source: str) -> dict:
    """Parse a bank SMS, store the credit, and auto-approve a matching pending payment."""
    parsed = parse_credit_text(text)
    if not parsed:
        return {"result": "unreadable"}
    amount, utr = parsed
    is_new = await record_credit(utr, amount, source, text)
    if not is_new:
        return {"result": "duplicate", "utr": utr}
    out = await auto_match(utr)
    out.setdefault("utr", utr)
    out["amount_paise_credit"] = amount
    return out
