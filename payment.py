"""CollectIQ monetisation helpers (Agent Payment Protocol, seller side).

Business model (subscription):
  - Reviewing invoices is always free.
  - Sending reminders needs an active plan:  $1 for the first 7 days (trial), then $10 per month (30 days).
  - Prices are in USD and paid in FET, converted with FET_PER_USD. The agent verifies the transfer on the
    Fetch.ai ledger before it activates the plan and sends anything.
All settings are read lazily so they work after load_env().
"""
import json
import os
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal

TIERS = {
    "trial": {"usd": Decimal("1"), "days": 7, "label": "7-day trial"},
    "monthly": {"usd": Decimal("10"), "days": 30, "label": "monthly plan"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def payments_enabled() -> bool:
    return os.environ.get("PAYMENTS", "off").strip().lower() in ("on", "true", "1", "fet")


def use_testnet() -> bool:
    return os.environ.get("FET_USE_TESTNET", "true").strip().lower() == "true"


def network_name() -> str:
    return "testnet" if use_testnet() else "mainnet"


def fet_per_usd() -> Decimal:
    """How many FET equal 1 USD. Testnet FET has no value, so the default is only a demo placeholder:
    on mainnet set it from the real FET/USD price."""
    return Decimal(os.environ.get("FET_PER_USD", "4"))


def usd_to_fet(usd: Decimal) -> Decimal:
    return (usd * fet_per_usd()).quantize(Decimal("0.0001"), rounding=ROUND_UP)


def fet_for_tier(tier: str) -> Decimal:
    return usd_to_fet(TIERS[tier]["usd"])


def fmt_fee(amount: Decimal) -> str:
    return f"{amount.normalize():f}"


class Subscriptions:
    """Who has paid, until when, and which transactions are already used.
    Supports both SQLite database (.db) and JSON (.json) storage so it survives restarts.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("SUBSCRIPTIONS_DB", os.environ.get("SUBSCRIPTIONS_FILE", "collectiq.db"))
        self.is_db = self.path.endswith(".db")
        if self.is_db:
            self._init_db()
        else:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}

    def _init_db(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_address TEXT PRIMARY KEY,
                    expires TEXT,
                    tier TEXT,
                    last_tx TEXT,
                    trial_used INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS used_transactions (
                    tx_hash TEXT PRIMARY KEY,
                    used_at TEXT
                )
            """)
            conn.commit()

    def _save(self):
        if not self.is_db:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f)
                os.replace(tmp, self.path)
            except OSError:
                pass  # in-memory state still works for this run

    def expiry(self, who: str):
        if self.is_db:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT expires FROM subscriptions WHERE user_address=?", (who,)).fetchone()
                return datetime.fromisoformat(row[0]) if row and row[0] else None
        raw = self.data.get(who, {}).get("expires")
        return datetime.fromisoformat(raw) if raw else None

    def is_active(self, who: str, now: datetime | None = None) -> bool:
        exp = self.expiry(who)
        return bool(exp and exp > (now or utcnow()))

    def next_tier(self, who: str) -> str:
        """The trial ($1 for 7 days) can be bought once per address; after that the monthly plan ($10/month)."""
        if self.is_db:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT trial_used FROM subscriptions WHERE user_address=?", (who,)).fetchone()
                return "monthly" if row and row[0] else "trial"
        return "monthly" if self.data.get(who, {}).get("trial_used") else "trial"

    def tx_used(self, tx: str) -> bool:
        if self.is_db:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT 1 FROM used_transactions WHERE tx_hash=?", (tx,)).fetchone()
                return row is not None
        return tx in self.data.get("_used_tx", [])

    def activate(self, who: str, tier: str, tx: str, now: datetime | None = None) -> datetime:
        now = now or utcnow()
        current = self.expiry(who)
        start = current if current and current > now else now
        expires = start + timedelta(days=TIERS[tier]["days"])
        if self.is_db:
            with sqlite3.connect(self.path) as conn:
                trial_val = 1 if tier == "trial" else 0
                conn.execute("""
                    INSERT INTO subscriptions (user_address, expires, tier, last_tx, trial_used)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_address) DO UPDATE SET
                        expires=excluded.expires,
                        tier=excluded.tier,
                        last_tx=excluded.last_tx,
                        trial_used=CASE WHEN excluded.tier='trial' THEN 1 ELSE subscriptions.trial_used END
                """, (who, expires.isoformat(), tier, tx, trial_val))
                conn.execute("""
                    INSERT OR IGNORE INTO used_transactions (tx_hash, used_at)
                    VALUES (?, ?)
                """, (tx, now.isoformat()))
                conn.commit()
            return expires

        rec = self.data.setdefault(who, {})
        rec.update(expires=expires.isoformat(), tier=tier, last_tx=tx)
        if tier == "trial":
            rec["trial_used"] = True
        self.data.setdefault("_used_tx", []).append(tx)
        self._save()
        return expires

    def status_line(self, who: str) -> str:
        exp = self.expiry(who)
        if exp and exp > utcnow():
            return f"Your plan is active until {exp:%d %b %Y}."
        t = TIERS[self.next_tier(who)]
        prefix = f"Your plan ended on {exp:%d %b %Y}. " if exp else "You have no plan yet. "
        return f"{prefix}Next: {t['label']} (${fmt_fee(t['usd'])}, {t['days']} days)."


def explain_pricing(status: str = "") -> str:
    t, m = TIERS["trial"], TIERS["monthly"]
    text = (f"Pricing: checking invoices and reviewing the plan is always free. To send reminders you need a plan: "
            f"${fmt_fee(t['usd'])} for the first {t['days']} days, then ${fmt_fee(m['usd'])} per month ({m['days']} days). "
            f"You pay in FET ({network_name()}), at {fmt_fee(fet_per_usd())} FET per USD, after you approve a review. "
            f"Nothing is sent until the payment is verified on-chain, and one payment unlocks unlimited runs for the period. "
            f"Payments are {'ON' if payments_enabled() else 'OFF (free demo mode)'}.")
    return f"{text}\n\n{status}" if status else text


async def request_payment(ctx, user_address: str, tier: str, amount: Decimal, wallet_address: str):
    """Send a RequestPayment to the user (shows Approve / Reject in ASI:One)."""
    from uagents_core.contrib.protocols.payment import Funds, RequestPayment

    t = TIERS[tier]
    request = RequestPayment(
        accepted_funds=[Funds(currency="FET", amount=fmt_fee(amount), payment_method="fet_direct")],
        recipient=wallet_address,
        deadline_seconds=300,
        reference=str(ctx.session),
        description=f"CollectIQ {t['label']}: ${fmt_fee(t['usd'])} for {t['days']} days of invoice reminders",
        metadata={
            "agent": "collectiq",
            "service": "invoice_payment_reminders_subscription",
            "plan": tier,
            "price_usd": fmt_fee(t["usd"]),
            "period_days": str(t["days"]),
            "fet_network": "stable-testnet" if use_testnet() else "mainnet",
            "content": f"Approve to start the {t['label']} and CollectIQ will send your reminders.",
        },
    )
    await ctx.send(user_address, request)


def verify_fet_payment(tx_id: str, expected: Decimal, buyer: str | None, recipient: str, logger) -> bool:
    """True only if the ledger shows a successful transfer buyer -> recipient of at least `expected` FET."""
    try:
        from cosmpy.aerial.client import LedgerClient, NetworkConfig

        testnet = use_testnet()
        config = NetworkConfig.fetchai_stable_testnet() if testnet else NetworkConfig.fetchai_mainnet()
        need = int(expected * (10 ** 18))
        denoms = ("atestfet", "afet")
        resp = LedgerClient(config).query_tx(tx_id)
        if not resp.is_successful():
            logger.error(f"Transaction {tx_id} was not successful")
            return False
        rec = amt = snd = False

        # Primary path: iterate over resp.events (dict-like structure)
        try:
            events_items = resp.events.items() if hasattr(resp.events, "items") else []
            for event_type, attrs in events_items:
                logger.info(f"Event type found: {event_type}")
                if event_type != "transfer":
                    continue
                if isinstance(attrs, dict):
                    attrs_list = [attrs]
                elif isinstance(attrs, list):
                    attrs_list = attrs
                else:
                    continue
                for a in attrs_list:
                    if a.get("recipient") != recipient:
                        continue
                    rec = True
                    tx_sender = a.get("sender")
                    if not buyer or tx_sender == buyer:
                        snd = True
                    raw = a.get("amount", "")
                    for d in denoms:
                        if raw.endswith(d):
                            try:
                                if int(raw[: -len(d)]) >= need:
                                    amt = True
                            except ValueError:
                                pass
        except Exception as events_err:
            logger.error(f"Events parsing failed ({events_err!r}), trying raw log fallback")

            # Fallback: inspect raw transaction logs when the events dict layout is unexpected
            try:
                raw_log = getattr(resp, "raw_log", None) or getattr(resp, "log", None) or ""
                if isinstance(raw_log, str) and recipient in raw_log and (not buyer or buyer in raw_log):
                    logger.info("Fallback raw-log check found recipient (and buyer) references")
                    rec = True
                    snd = True
            except Exception as fallback_err:
                logger.error(f"Raw-log fallback also failed: {fallback_err!r}")

        if rec and amt and snd:
            logger.info(f"Payment verified: {tx_id}")
            return True
        logger.error(f"Payment check failed (recipient={rec}, amount={amt}, sender={snd})")
        return False
    except Exception as e:  # never crash the agent on a verification problem
        logger.error(f"FET verification error: {e!r}\n{traceback.format_exc()}")
        return False
