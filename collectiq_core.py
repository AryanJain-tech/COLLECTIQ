"""CollectIQ core: an accounts-receivable assistant's brain.

Flow:  Store (SQLite) -> build_plan() [no side effects] -> human approves
       -> execute_plan() [sends + logs] -> summarize().

Design rules (they are the point of the project):
- Python decides WHO gets a reminder and for HOW MUCH. Text may be polished by an
  AI, but the guard rejects any text that drops the facts or invents links,
  penalties, discounts, bank details or legal threats.
- Every real send attempt is written to an audit log; the log also prevents duplicates.
- Only TEMPORARY failures are retried. If the provider's answer is unknown
  (timeout), we never auto-resend, because the message may already be delivered.
- Without credentials, channels run in a clearly labelled SANDBOX mode.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4

import requests

STATUSES = {"unpaid", "partially_paid", "paid", "disputed"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FORBIDDEN = re.compile(
    r"https?://|www\.|\bupi\b|\bbank\b|account number|ifsc|penalt|late fee|"
    r"\binterest\b|discount|legal|lawyer|court|police",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- config
@dataclass
class Policy:
    remind_due_soon: bool = False  # polite advance reminders for not-yet-due invoices
    due_soon_days: int = 3
    cooldown_days: int = 3         # no second reminder for the same invoice+channel inside this window
    max_retries: int = 2           # retries for TEMPORARY failures only
    retry_delay_s: float = 1.0
    high_value: float = 25000      # outstanding amount that gets flagged for the owner
    firm_after_days: int = 7       # overdue longer than this -> firm (still professional) tone


def load_env(path: str = ".env") -> None:
    """Tiny .env reader so Windows users don't need extra libraries."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- formatting helpers
def inr(x) -> str:
    """₹ with Indian digit grouping: 100000 -> ₹1,00,000"""
    cents = round(float(x) * 100)
    whole, frac = divmod(cents, 100)
    s = str(int(whole))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head) + "," + tail
    return "₹" + s + (f".{int(frac):02d}" if frac else "")


def fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def parse_date(s):
    try:
        return date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def clean_phone(p, default_cc: str = "91"):
    digits = re.sub(r"[\s\-()]", "", str(p or ""))
    if not re.fullmatch(r"\+?\d{10,15}", digits):
        return None
    digits = digits.lstrip("+")
    return default_cc + digits if len(digits) == 10 else digits


# --------------------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
  invoice_id TEXT, invoice_number TEXT, customer_name TEXT, email TEXT, whatsapp TEXT,
  email_opt_in INTEGER DEFAULT 1, whatsapp_opt_in INTEGER DEFAULT 1,
  total REAL, paid REAL DEFAULT 0, issue_date TEXT, due_date TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS reminder_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT, invoice_number TEXT,
  customer_name TEXT, channel TEXT, message_type TEXT, ts TEXT, status TEXT, mode TEXT,
  provider_ref TEXT, error TEXT, attempts INTEGER, next_eligible TEXT);
"""


class Store:
    def __init__(self, path: str = "collectiq.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def invoices(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM invoices ORDER BY rowid")]

    def add_invoice(self, **kw) -> None:
        cols = ["invoice_id", "invoice_number", "customer_name", "email", "whatsapp",
                "email_opt_in", "whatsapp_opt_in", "total", "paid", "issue_date",
                "due_date", "status"]
        vals = [kw.get(c, 1 if c.endswith("opt_in") else (0 if c == "paid" else None)) for c in cols]
        self.db.execute(f"INSERT INTO invoices ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        self.db.commit()

    def delete_invoice(self, number: str) -> int:
        cur = self.db.execute("DELETE FROM invoices WHERE lower(invoice_number)=lower(?)", (number,))
        self.db.commit()
        return cur.rowcount

    def upsert_invoice(self, **kw) -> bool:
        """Insert an invoice, or replace the one with the same number. True when it replaced one."""
        replaced = self.delete_invoice(kw["invoice_number"]) > 0
        kw.setdefault("invoice_id", kw["invoice_number"])
        self.add_invoice(**kw)
        return replaced

    def record_payment(self, number: str, amount=None):
        """Customer paid: add `amount` (or the whole balance when None). Returns (paid, total, status) or None."""
        row = self.db.execute("SELECT total, paid FROM invoices WHERE lower(invoice_number)=lower(?) ORDER BY rowid LIMIT 1",
                              (number,)).fetchone()
        if not row:
            return None
        total, paid = float(row["total"] or 0), float(row["paid"] or 0)
        paid = total if amount is None else min(total, paid + float(amount))
        status = "paid" if paid >= total else "partially_paid"
        self.db.execute("UPDATE invoices SET paid=?, status=? WHERE lower(invoice_number)=lower(?)", (paid, status, number))
        self.db.commit()
        return paid, total, status

    def clear(self, log: bool = True) -> None:
        self.db.execute("DELETE FROM invoices")
        if log:
            self.db.execute("DELETE FROM reminder_log")
        self.db.commit()

    def log(self, **kw) -> None:
        cols = ["invoice_id", "invoice_number", "customer_name", "channel", "message_type",
                "ts", "status", "mode", "provider_ref", "error", "attempts", "next_eligible"]
        self.db.execute(f"INSERT INTO reminder_log ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [kw.get(c) for c in cols])
        self.db.commit()

    def recent_attempt(self, invoice_id: str, channel: str, since_iso: str):
        """An accepted OR unknown-outcome send inside the cooldown window blocks a new one."""
        row = self.db.execute(
            "SELECT * FROM reminder_log WHERE invoice_id=? AND channel=? "
            "AND status IN ('accepted','unknown') AND substr(ts,1,10) >= ? ORDER BY id DESC LIMIT 1",
            (invoice_id, channel, since_iso)).fetchone()
        return dict(row) if row else None

    def recent_log(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM reminder_log ORDER BY id DESC LIMIT ?", (limit,))]


def seed_demo(store: Store, today: date) -> None:
    """Sample invoices covering every case. Dates are relative to `today`, so the demo never goes stale."""
    store.clear()
    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    rows = [
        ("1", "INV-101", "Rahul Sharma", "rahul@example.com", "9876543210", 1, 1, 5000, 0, d(-34), d(-4), "unpaid"),
        ("2", "INV-102", "Priya Verma", "priya@example.com", "9876543211", 1, 1, 12000, 12000, d(-40), d(-10), "paid"),
        ("3", "INV-103", "Amit Traders", "amit@example.com", "9876543212", 1, 1, 10000, 4000, d(-45), d(-10), "partially_paid"),
        ("4", "INV-104", "Neha Gupta", "neha@example.com", "9876543213", 1, 1, 7500, 0, d(-30), d(0), "unpaid"),
        ("5", "INV-105", "Sunil Kumar", "sunil@example.com", "9876540000", 1, 1, 30000, 0, d(-50), d(-20), "unpaid"),
        ("6", "INV-106", "Kavita Singh", "kavita@example.com", "9876543214", 1, 1, 15000, 0, d(-25), d(-6), "disputed"),
        ("7", "INV-107", "Rohit Jain", None, "9876543215", 1, 1, 4000, 0, d(-20), d(-2), "unpaid"),
        ("8", "INV-108", "Meera Das", "meera@example.com", None, 1, 1, 8000, 0, d(-20), d(-3), "unpaid"),
        ("9", "INV-109", "Vikram Rao", "vikram@example.com", "9876543216", 1, 1, 9000, 0, d(-10), d(2), "unpaid"),
        ("10", "INV-110", "Anil Mehta", "anil@bounce.test", "9876543217", 1, 1, 6500, 0, d(-30), d(-2), "unpaid"),
        ("11", "INV-101", "Rahul Sharma", "rahul@example.com", "9876543210", 1, 1, 5000, 0, d(-34), d(-4), "unpaid"),
        ("12", "INV-112", "Sameer Khan", "sameer@example.com", "9876543218", 1, 1, 5000, 0, d(-20), d(-5), "pending???"),
        ("13", "INV-113", "Divya Nair", "divya@example.com", "9876543219", 1, 0, 3500, 0, d(-20), d(-8), "unpaid"),
    ]
    keys = ["invoice_id", "invoice_number", "customer_name", "email", "whatsapp", "email_opt_in",
            "whatsapp_opt_in", "total", "paid", "issue_date", "due_date", "status"]
    for r in rows:
        store.add_invoice(**dict(zip(keys, r)))


# --------------------------------------------------------------------------- classification
def classify(inv: dict, today: date, policy: Policy, dup_of: str | None = None) -> dict:
    """Validate one invoice and decide its category. No side effects."""
    problems: list[str] = []
    if not str(inv.get("invoice_id") or "").strip():
        problems.append("missing invoice id")
    total, paid = _num(inv.get("total")), _num(inv.get("paid") or 0)
    if total is None or total <= 0:
        problems.append("invalid invoice amount")
    if paid is None or paid < 0:
        problems.append("invalid amount paid")
    due = parse_date(inv.get("due_date"))
    if due is None:
        problems.append("invalid due date")
    status = str(inv.get("status") or "").strip().lower()
    if status not in STATUSES:
        problems.append(f"unknown payment status '{inv.get('status')}'")
    if dup_of:
        problems.append(f"duplicate of invoice {dup_of}")

    outstanding = None
    if total is not None and paid is not None and total > 0 and paid >= 0:
        outstanding = round(total - paid, 2)  # what the reminder must mention
        if status == "paid" and outstanding > 0:
            problems.append("marked paid but a balance remains")
        if status in ("unpaid", "partially_paid") and outstanding <= 0:
            problems.append("marked unpaid but nothing is outstanding")

    base = {"outstanding": outstanding, "due": due, "problems": problems, "days_overdue": None}
    if problems:
        return {**base, "category": "needs_review"}
    if status == "paid":
        return {**base, "category": "paid"}
    if status == "disputed":
        return {**base, "category": "disputed"}
    days = (today - due).days
    base["days_overdue"] = days
    if days > 0:
        cat = "overdue"
    elif days == 0:
        cat = "due_today"
    elif -days <= policy.due_soon_days:
        cat = "due_soon"
    else:
        cat = "not_due"
    return {**base, "category": cat}


# --------------------------------------------------------------------------- messages
def tone_for(category: str, days_overdue: int, policy: Policy) -> str:
    if category in ("due_today", "due_soon"):
        return "friendly"
    return "firm" if days_overdue > policy.firm_after_days else "follow_up"


def template_message(name: str, number: str, outstanding: float, due: date, days: int, tone: str, business: str) -> str:
    amt, when = inr(outstanding), fmt_date(due)
    if tone == "friendly":
        return (f"Hello {name}, this is a friendly reminder that invoice {number} with an outstanding "
                f"balance of {amt} is due on {when}. If you have already made the payment, please "
                f"disregard this message or contact us if you need assistance. Thank you. - {business}")
    if tone == "follow_up":
        return (f"Hello {name}, our records show that invoice {number} was due on {when} and an "
                f"outstanding balance of {amt} remains. If you have already made the payment, please "
                f"disregard this message or contact us if you need assistance. Thank you. - {business}")
    return (f"Hello {name}, invoice {number} has been overdue since {when} ({days} days) with an "
            f"outstanding balance of {amt}. Please arrange payment at your earliest convenience, or "
            f"contact us if there is an issue with this invoice. If you have already paid, please "
            f"disregard this message. Regards, {business}")


def guard_message(text: str, number: str, outstanding: float, due: date) -> list[str]:
    """Reject text that lost a verified fact or invented something. Empty list = safe to send."""
    bad = []
    if number not in text:
        bad.append("invoice number missing")
    if inr(outstanding) not in text:
        bad.append("outstanding amount missing or changed")
    if fmt_date(due) not in text:
        bad.append("due date missing")
    m = FORBIDDEN.search(text)
    if m:
        bad.append(f"forbidden content: '{m.group(0)}'")
    return bad


def generate_message(facts: dict, business: str, rewriter=None) -> tuple[str, str]:
    """Template first; an optional AI `rewriter(text)->text` may polish it, but only if the guard passes."""
    base = template_message(facts["name"], facts["number"], facts["outstanding"], facts["due"],
                            facts["days"], facts["tone"], business)
    if rewriter:
        try:
            alt = rewriter(base)
            if alt and not guard_message(alt, facts["number"], facts["outstanding"], facts["due"]):
                return alt, "ai_polished"
        except Exception:
            pass  # any rewriter problem -> safe template
    return base, "template"


# --------------------------------------------------------------------------- channels
@dataclass
class SendResult:
    status: str  # accepted | failed_permanent | failed_temporary | unknown
    ref: str | None = None
    error: str | None = None
    mode: str = "sandbox"


class SandboxChannel:
    """Pretend sender. Writes what WOULD be sent to outbox.jsonl. Never contacts anyone.

    Demo failure rules (so judges can see error handling):
      email to *@bounce.test        -> permanent failure (provider rejects)
      email starting with 'timeout' -> unknown outcome (never auto-resent)
      WhatsApp number ending 0000   -> temporary failure on 1st try, accepted on retry
    """
    mode = "sandbox"

    def __init__(self, name: str, outbox_path: str = "outbox.jsonl"):
        self.name, self.outbox_path, self._tries = name, outbox_path, {}

    def send(self, to, subject, body, meta=None) -> SendResult:
        n = self._tries[to] = self._tries.get(to, 0) + 1
        if self.name == "email" and to.lower().endswith("@bounce.test"):
            return SendResult("failed_permanent", error="provider rejected: mailbox does not exist")
        if self.name == "email" and to.lower().startswith("timeout"):
            return SendResult("unknown", error="timed out waiting for provider; delivery state unknown")
        if self.name == "whatsapp" and to.endswith("0000") and n == 1:
            return SendResult("failed_temporary", error="503 provider temporarily unavailable")
        ref = f"sandbox-{self.name}-{uuid4().hex[:8]}"
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "channel": self.name,
                                "to": to, "subject": subject, "body": body, "ref": ref,
                                "SANDBOX": "not actually sent"}, ensure_ascii=False) + "\n")
        return SendResult("accepted", ref=ref)


class SmtpEmailChannel:
    """Real email through any SMTP provider. Not exercised by the automated tests."""
    name, mode = "email", "live"

    def __init__(self, host, port, user, password, sender):
        self.host, self.port, self.user, self.password, self.sender = host, int(port), user, password, sender

    def send(self, to, subject, body, meta=None) -> SendResult:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = self.sender, to, subject, make_msgid()
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as s:
                s.starttls()
                s.login(self.user, self.password)
                s.send_message(msg)
            return SendResult("accepted", ref=msg["Message-ID"], mode="live")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPAuthenticationError) as e:
            return SendResult("failed_permanent", error=str(e)[:200], mode="live")
        except smtplib.SMTPResponseException as e:
            st = "failed_permanent" if e.smtp_code >= 500 else "failed_temporary"
            return SendResult(st, error=f"{e.smtp_code} {e.smtp_error!r}"[:200], mode="live")
        except (socket.timeout, TimeoutError):
            return SendResult("unknown", error="timeout; delivery state unknown", mode="live")
        except OSError as e:  # could not connect at all -> nothing was sent
            return SendResult("failed_temporary", error=f"connection problem: {e}"[:200], mode="live")


class WhatsAppCloudChannel:
    """WhatsApp Business Cloud API (Meta). Business-initiated messages must use an APPROVED template
    whose body has 4 variables: {{1}} name, {{2}} invoice number, {{3}} outstanding, {{4}} due date.
    Not exercised by the automated tests."""
    name, mode = "whatsapp", "live"

    def __init__(self, token, phone_number_id, template, lang="en", version="v21.0"):
        self.token, self.template, self.lang = token, template, lang
        self.url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"

    def send(self, to, subject, body, meta=None) -> SendResult:
        params = (meta or {}).get("params", [])
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": self.template, "language": {"code": self.lang},
                                "components": [{"type": "body",
                                                "parameters": [{"type": "text", "text": str(p)} for p in params]}]}}
        try:
            r = requests.post(self.url, headers={"Authorization": f"Bearer {self.token}"}, json=payload, timeout=15)
        except requests.Timeout:
            return SendResult("unknown", error="timeout; delivery state unknown", mode="live")
        except requests.ConnectionError as e:
            return SendResult("failed_temporary", error=f"connection problem: {e}"[:200], mode="live")
        if r.status_code == 200:
            try:
                return SendResult("accepted", ref=r.json()["messages"][0]["id"], mode="live")
            except (KeyError, IndexError, ValueError):
                return SendResult("unknown", error="200 but unexpected response", mode="live")
        err = ""
        try:
            err = r.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        st = "failed_temporary" if (r.status_code == 429 or r.status_code >= 500) else "failed_permanent"
        return SendResult(st, error=f"{r.status_code} {err}"[:200], mode="live")


# Optional Twilio import — only needed when TwilioWhatsAppChannel is actually used
try:
    from twilio.rest import Client as _TwilioClient
    from twilio.base.exceptions import TwilioRestException as _TwilioRestException
except ImportError:
    _TwilioClient = None
    _TwilioRestException = None


class TwilioWhatsAppChannel:
    """WhatsApp via the Twilio Programmable Messaging API.

    Supports two modes:
      - Template message: when ``content_sid`` is set, sends a template message
        with ``content_variables`` (Twilio Content API / pre-approved template).
      - Freeform message: when ``content_sid`` is None, sends a plain text body
        (works in the Twilio sandbox and for session messages).
    """
    name, mode = "whatsapp", "live"

    def __init__(self, account_sid: str, auth_token: str, from_number: str, content_sid: str | None = None):
        if _TwilioClient is None:
            raise ImportError("The 'twilio' package is required for TwilioWhatsAppChannel. Install it with: pip install twilio")
        self.client = _TwilioClient(account_sid, auth_token)
        # from_number may already include the 'whatsapp:' prefix
        self.from_number = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
        self.content_sid = content_sid

    def send(self, to, subject, body, meta=None) -> SendResult:
        # Format the destination number for Twilio (ensure exactly one whatsapp:+ prefix)
        cleaned = str(to).removeprefix("whatsapp:").lstrip("+").strip()
        to_wa = f"whatsapp:+{cleaned}"

        try:
            params = (meta or {}).get("params", [])
            if self.content_sid and params:
                # Template message via Twilio Content API
                # Map params list to content_variables dict: {"1": "...", "2": "..."}
                content_vars = {}
                if len(params) >= 2:
                    # Combine invoice number + amount into variable 1, due date into variable 2
                    # (matches the format used in try_twilio.py)
                    content_vars = {"1": f"{params[1]} ({params[2]})" if len(params) >= 3 else str(params[1]),
                                    "2": str(params[3]) if len(params) >= 4 else ""}
                else:
                    content_vars = {str(i + 1): str(p) for i, p in enumerate(params)}

                msg = self.client.messages.create(
                    from_=self.from_number,
                    to=to_wa,
                    content_sid=self.content_sid,
                    content_variables=json.dumps(content_vars),
                )
            else:
                # Freeform text message (for custom messages, sandbox, or session messages)
                msg = self.client.messages.create(
                    from_=self.from_number,
                    to=to_wa,
                    body=body,
                )
            return SendResult("accepted", ref=msg.sid, mode="live")

        except Exception as e:
            # Handle TwilioRestException specifically if available
            if _TwilioRestException is not None and isinstance(e, _TwilioRestException):
                raw_msg = getattr(e, "msg", None) or str(e)
                clean_err = re.sub(r"\x1b\[[0-9;]*m", "", raw_msg).strip()
                code = getattr(e, "code", None)
                if code == 572002 or "verified recipient" in clean_err.lower():
                    clean_err = "Twilio Trial: number not verified. Add to Twilio Console -> Verified Caller IDs to send in trial mode."
                elif code == 63015:
                    clean_err = "Twilio Sandbox: recipient has not joined the WhatsApp sandbox."
                elif code in (21211, 21614):
                    clean_err = "Invalid phone number format or not WhatsApp enabled."

                st = "failed_permanent" if (400 <= e.status < 500) else "failed_temporary"
                return SendResult(st, error=clean_err[:250], mode="live")
            # Timeouts
            if isinstance(e, (socket.timeout, TimeoutError)):
                return SendResult("unknown", error="timeout; delivery state unknown", mode="live")
            # Connection errors
            if isinstance(e, (ConnectionError, OSError)):
                return SendResult("failed_temporary", error=f"connection problem: {e}"[:200], mode="live")
            # Unknown / unexpected error
            return SendResult("unknown", error=f"unexpected error: {e}"[:200], mode="live")


def build_channels(outbox_path: str = "outbox.jsonl") -> dict:
    """Live channel if credentials exist in the environment, otherwise a labelled sandbox.

    WhatsApp priority:
      1. WhatsApp Cloud API (Meta) — if WHATSAPP_TOKEN etc. are set
      2. Twilio WhatsApp — if TWILIO_ACCOUNT_SID etc. are set
      3. Sandbox
    """
    e = os.environ
    if all(e.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")):
        email = SmtpEmailChannel(e["SMTP_HOST"], e.get("SMTP_PORT", "587"), e["SMTP_USER"], e["SMTP_PASS"], e["SMTP_FROM"])
    else:
        email = SandboxChannel("email", outbox_path)
    if all(e.get(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TEMPLATE")):
        wa = WhatsAppCloudChannel(e["WHATSAPP_TOKEN"], e["WHATSAPP_PHONE_NUMBER_ID"], e["WHATSAPP_TEMPLATE"],
                                  e.get("WHATSAPP_LANG", "en"), e.get("WHATSAPP_API_VERSION", "v21.0"))
    elif all(e.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")) and _TwilioClient is not None:
        wa = TwilioWhatsAppChannel(e["TWILIO_ACCOUNT_SID"], e["TWILIO_AUTH_TOKEN"], e["TWILIO_WHATSAPP_FROM"],
                                   e.get("TWILIO_CONTENT_SID"))
    else:
        wa = SandboxChannel("whatsapp", outbox_path)
    return {"email": email, "whatsapp": wa}


# --------------------------------------------------------------------------- plan (no side effects)
def _channel_entry(name, addr, valid, opted_in, store, inv_id, since):
    if not addr:
        return {"channel": name, "to": None, "action": "skip", "reason": f"no {name} contact on file"}
    if not valid:
        return {"channel": name, "to": addr, "action": "skip", "reason": f"invalid {name} contact"}
    if not opted_in:
        return {"channel": name, "to": addr, "action": "skip", "reason": f"customer has not agreed to {name} reminders"}
    prev = store.recent_attempt(inv_id, name, since)
    if prev:
        return {"channel": name, "to": addr, "action": "skip", "reason": f"already reminded on {prev['ts'][:10]}"}
    return {"channel": name, "to": addr, "action": "send", "reason": ""}


def build_plan(store: Store, today: date, policy: Policy | None = None, rewriter=None, business: str | None = None, target: str | None = None) -> dict:
    policy = policy or Policy()
    business = business or os.getenv("BUSINESS_NAME", "our team")
    since = (today - timedelta(days=policy.cooldown_days - 1)).isoformat()
    rows = store.invoices()
    if target:
        t_clean = target.strip().lower()
        rows = [r for r in rows if t_clean in (r.get("invoice_number") or "").lower() or t_clean in (r.get("customer_name") or "").lower()]
    seen: dict[str, str] = {}
    plan = {"today": today.isoformat(), "checked": len(rows), "paid": 0, "disputed": [], "review": [],
            "not_due": 0, "items": []}
    for r in rows:
        key = (r.get("invoice_number") or "").strip().upper()
        dup_of = seen.get(key) if key else None
        if key and key not in seen:
            seen[key] = r.get("invoice_number")
        c = classify(r, today, policy, dup_of)
        cat = c["category"]
        who = {"invoice_id": r.get("invoice_id"), "invoice_number": r.get("invoice_number"),
               "customer": r.get("customer_name")}
        if cat == "paid":
            plan["paid"] += 1
            continue
        if cat == "needs_review":
            plan["review"].append({**who, "problems": c["problems"]})
            continue
        if cat == "disputed":
            plan["disputed"].append({**who, "outstanding": c["outstanding"]})
            continue
        if not (cat in ("overdue", "due_today") or (cat == "due_soon" and policy.remind_due_soon)):
            plan["not_due"] += 1
            continue
        days = max(c["days_overdue"], 0)
        tone = tone_for(cat, days, policy)
        facts = {"name": r["customer_name"], "number": r["invoice_number"], "outstanding": c["outstanding"],
                 "due": c["due"], "days": days, "tone": tone}
        body, how = generate_message(facts, business, rewriter)
        email_ok = bool(EMAIL_RE.match(r.get("email") or ""))
        phone = clean_phone(r.get("whatsapp"))
        item = {**who, "outstanding": c["outstanding"], "due_date": c["due"].isoformat(), "days_overdue": days,
                "category": cat, "tone": tone, "high_value": c["outstanding"] >= policy.high_value,
                "message_source": how,
                "channels": [
                    _channel_entry("email", r.get("email"), email_ok, r.get("email_opt_in", 1), store, r["invoice_id"], since),
                    _channel_entry("whatsapp", phone or r.get("whatsapp"), bool(phone), r.get("whatsapp_opt_in", 1), store, r["invoice_id"], since),
                ],
                "messages": {
                    "email": {"subject": f"Payment Reminder - Invoice {r['invoice_number']}", "body": body},
                    "whatsapp": {"body": body, "params": [r["customer_name"], r["invoice_number"],
                                                          inr(c["outstanding"]), fmt_date(c["due"])]}}}
        plan["items"].append(item)
    plan["items"].sort(key=lambda i: (not i["high_value"], -i["days_overdue"], -i["outstanding"]))
    return plan


def planned_sends(plan: dict) -> dict:
    n = {"email": 0, "whatsapp": 0, "invoices": 0}
    for it in plan["items"]:
        sends = [c for c in it["channels"] if c["action"] == "send"]
        n["invoices"] += bool(sends)
        for c in sends:
            n[c["channel"]] += 1
    return n


# --------------------------------------------------------------------------- execute
def _send_with_retry(channel, to, msg, policy: Policy):
    attempts = 0
    while True:
        attempts += 1
        try:
            res = channel.send(to, msg.get("subject"), msg["body"], msg)
        except Exception as e:  # a crash mid-send = outcome unknown -> never auto-resend
            res = SendResult("unknown", error=f"unexpected error: {e}", mode=getattr(channel, "mode", "unknown"))
        if res.status == "failed_temporary" and attempts <= policy.max_retries:
            time.sleep(policy.retry_delay_s * attempts)
            continue
        return res, attempts


def execute_plan(store: Store, plan: dict, channels: dict, policy: Policy | None = None, today: date | None = None) -> list[dict]:
    policy = policy or Policy()
    today = today or date.fromisoformat(plan["today"])
    since = (today - timedelta(days=policy.cooldown_days - 1)).isoformat()
    results = []
    for item in plan["items"]:
        for ch in item["channels"]:
            if ch["action"] != "send":
                continue
            name = ch["channel"]
            base = {"invoice_id": item["invoice_id"], "invoice_number": item["invoice_number"],
                    "customer": item["customer"], "channel": name}
            if store.recent_attempt(item["invoice_id"], name, since):  # last-second duplicate guard
                results.append({**base, "status": "skipped_duplicate", "attempts": 0, "error": None, "mode": channels[name].mode})
                continue
            res, attempts = _send_with_retry(channels[name], ch["to"], item["messages"][name], policy)
            nxt = (today + timedelta(days=policy.cooldown_days)).isoformat() if res.status in ("accepted", "unknown") else today.isoformat()
            store.log(invoice_id=item["invoice_id"], invoice_number=item["invoice_number"], customer_name=item["customer"],
                      channel=name, message_type=item["tone"], ts=f"{today.isoformat()}T{datetime.now():%H:%M:%S}",
                      status=res.status, mode=res.mode, provider_ref=res.ref, error=res.error,
                      attempts=attempts, next_eligible=nxt)
            results.append({**base, "status": res.status, "attempts": attempts, "error": res.error, "mode": res.mode, "ref": res.ref})
    return results


# --------------------------------------------------------------------------- summary + text
def summarize(plan: dict, results: list[dict], channels: dict | None = None) -> dict:
    s = {"checked": plan["checked"], "paid_excluded": plan["paid"], "flagged_for_review": len(plan["review"]),
         "disputed_paused": len(plan["disputed"]), "not_yet_due": plan["not_due"],
         "invoices_reminded": len({r["invoice_id"] for r in results if r["status"] in ("accepted", "unknown")}),
         "channels": {}, "failures": []}
    for name in ("email", "whatsapp"):
        rs = [r for r in results if r["channel"] == name]
        s["channels"][name] = {
            "accepted": sum(r["status"] == "accepted" for r in rs),
            "failed": sum(r["status"].startswith("failed") for r in rs),
            "unknown": sum(r["status"] == "unknown" for r in rs),
            "duplicates_prevented": sum(r["status"] == "skipped_duplicate" for r in rs),
            "retried": sum(r["attempts"] > 1 for r in rs),
            "mode": (channels[name].mode if channels else (rs[0]["mode"] if rs else "n/a"))}
    for r in results:
        if r["status"] not in ("accepted", "skipped_duplicate"):
            s["failures"].append(f'{r["invoice_number"]} ({r["customer"]}) via {r["channel"]}: {r["status"]} - {r["error"]}')
    return s


def _mode_label(mode: str) -> str:
    return "SANDBOX (nothing really sent)" if mode == "sandbox" else mode.upper()


def _pl(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def format_plan(plan: dict) -> str:
    n = planned_sends(plan)
    out = [f"Checked {plan['checked']} invoices on {fmt_date(date.fromisoformat(plan['today']))}.",
           f"{_pl(n['invoices'], 'invoice needs', 'invoices need')} a reminder "
           f"({_pl(n['email'], 'email', 'emails')}, {_pl(n['whatsapp'], 'WhatsApp message', 'WhatsApp messages')})."]
    lines, idle = [], 0
    for it in plan["items"]:
        sends = [c["channel"] for c in it["channels"] if c["action"] == "send"]
        if not sends:
            idle += 1
            continue
        age = "due today" if it["days_overdue"] == 0 else f"{it['days_overdue']} days overdue"
        star = " [HIGH VALUE]" if it["high_value"] else ""
        lines.append(f"- {it['invoice_number']} | {it['customer']} | {inr(it['outstanding'])} | {age} | {it['tone']} | {', '.join(sends)}{star}")
        for c in it["channels"]:
            if c["action"] == "skip":
                lines.append(f"  (skipped {c['channel']}: {c['reason']})")
    if lines:
        out.append("\n".join(lines))
    if idle:
        out.append(f"{_pl(idle, 'invoice has', 'invoices have')} nothing to send (already reminded recently or no usable contact).")
    if plan["paid"]:
        out.append(f"Excluded as already paid: {plan['paid']}.")
    if plan["disputed"]:
        out.append("Paused (disputed, needs your review): " + ", ".join(d["invoice_number"] for d in plan["disputed"]) + ".")
    if plan["review"]:
        out.append("Flagged for review instead of sending:\n" + "\n".join(
            f"- {r['invoice_number']} ({r['customer']}): {'; '.join(r['problems'])}" for r in plan["review"]))
    if plan["not_due"]:
        out.append(f"Not due yet: {plan['not_due']}.")
    return "\n\n".join(out)


def format_summary(s: dict) -> str:
    out = ["Invoice processing completed.",
           f"Invoices checked: {s['checked']}\n- Paid (excluded): {s['paid_excluded']}\n- Reminded: {s['invoices_reminded']}\n"
           f"- Flagged for review: {s['flagged_for_review']}\n- Disputed (no reminder sent): {s['disputed_paused']}"]
    for name, label in (("email", "Email"), ("whatsapp", "WhatsApp")):
        c = s["channels"][name]
        extra = f", delivery unknown: {c['unknown']}" if c["unknown"] else ""
        extra += f", recovered by retry: {c['retried']}" if c["retried"] else ""
        extra += f", duplicates prevented: {c['duplicates_prevented']}" if c["duplicates_prevented"] else ""
        out.append(f"{label} [{_mode_label(c['mode'])}]\n- Accepted by provider: {c['accepted']}\n- Failed: {c['failed']}{extra}")
    if s["failures"]:
        out.append("Failures and reasons:\n" + "\n".join(f"- {f}" for f in s["failures"]))
    out.append("'Accepted' means the provider took the message, not that the customer has read it.")
    return "\n\n".join(out)


def format_history(rows: list[dict]) -> str:
    if not rows:
        return "No reminders have been sent yet."
    return "Latest send attempts:\n\n" + "\n".join(
        f"- {r['ts'][:16].replace('T', ' ')} | {r['invoice_number']} | {r['channel']} | {r['status']}"
        f"{' (' + str(r['attempts']) + ' attempts)' if (r['attempts'] or 1) > 1 else ''} | {r['mode']}" for r in rows)
