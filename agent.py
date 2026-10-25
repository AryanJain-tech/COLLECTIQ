"""CollectIQ agent for ASI:One (Agent Chat Protocol + Interactive Cards + Agent Payment Protocol).

Conversation:
  you:   "Check all invoices and send reminders to everyone due or overdue"
  agent: shows what it found + a REVIEW CARD (Send / Cancel)      <- nothing sent yet, review is free
  you:   tap Send (or type "confirm")
  agent: (PAYMENTS=on) asks for a small FET fee via the Payment Protocol, verifies it on-chain
  agent: sends via email + WhatsApp, logs everything, shows the summary

Run:  python agent.py     (see README.md for the mailbox / Agentverse steps)
"""
import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RejectPayment,
    payment_protocol_spec,
)

from collectiq_core import (
    Policy, Store, build_channels, build_plan, clean_phone, execute_plan, format_history, format_plan,
    format_summary, inr, load_env, planned_sends, seed_demo, summarize,
)
from intake import HOW_TO_ADD, format_import_report, format_invoice_list, import_invoices
from file_intake import detect_import_intent, import_from_file
from payment import (
    TIERS, Subscriptions, explain_pricing, fet_for_tier, fmt_fee, network_name, payments_enabled, request_payment,
    verify_fet_payment,
)

load_env()

agent: Agent | None = None


def get_agent() -> Agent:
    global agent
    if agent is None:
        agent = Agent(
            name="collectiq",
            seed=os.environ.get("AGENT_SEED", "CHANGE-ME-set-AGENT_SEED-in-your-env-file"),
            port=int(os.environ.get("AGENT_PORT", "8002")),
            mailbox=os.environ.get("AGENT_MAILBOX", "true").lower() in ("true", "1", "yes"),
            description="CollectIQ checks your invoices and sends personalised payment reminders by email and WhatsApp, with your approval.",
            readme_path="AGENT_README.md" if os.path.exists("AGENT_README.md") else None,
            publish_agent_details=True,
        )
        agent.include(chat_proto, publish_manifest=True)
        agent.include(payment_proto, publish_manifest=True)
    return agent


chat_proto = Protocol(spec=chat_protocol_spec)
payment_proto = Protocol(spec=payment_protocol_spec, role="seller")

DATA_DIR = os.environ.get("COLLECTIQ_DATA_DIR", "data")
_STORES: dict[str, Store] = {}


def store_for(sender: str) -> Store:
    """Every chat user gets their own private invoice database, so customers never see each other's data."""
    if sender not in _STORES:
        os.makedirs(DATA_DIR, exist_ok=True)
        _STORES[sender] = Store(os.path.join(DATA_DIR, hashlib.sha256(sender.encode()).hexdigest()[:16] + ".db"))
    return _STORES[sender]


CHANNELS = build_channels()
POLICY = Policy()
PENDING: dict[str, dict] = {}   # sender address -> plan waiting for approval
AWAITING: dict[str, dict] = {}  # sender -> {"plan", "tier", "amount"} waiting for payment
TO_REQUEST: dict[str, tuple] = {}  # sender -> (tier, amount): send a RequestPayment after the chat reply
SUBS = Subscriptions()          # who has an active plan and which transactions are used (subscriptions.json)
EARNINGS: list[dict] = []       # verified payments received

HELP = (
    "👋 **Hello! I'm CollectIQ**, your AI Accounts Receivable Assistant.\n\n"
    "I track due and overdue invoices, calculate exact balances, and send personalized payment reminders via WhatsApp and Email.\n\n"
    "Here is how you can interact with me in plain English:\n\n"
    "📁 **1. Import Invoices**\n"
    "   • Paste rows directly into chat\n"
    "   • `import csv d:\\invoice.csv`\n"
    "   • `import excel d:\\data.xlsx`\n"
    "   • `import pdf d:\\bill.pdf`\n"
    "   • `import google sheet https://docs.google.com/...`\n"
    "   • Or compound: `import csv d:\\invoice.csv and send reminders to all`\n\n"
    "📋 **2. Check Invoices & Status**\n"
    "   • `my invoices` — list all your invoices and balances\n"
    "   • `status` — check who is due, overdue, or paid\n\n"
    "⚡ **3. Send Payment Reminders (with Human Approval)**\n"
    "   • `send reminders to everyone` (or `send remainder to all`)\n"
    "   • `send reminders through whatsapp and email`\n"
    "   • I calculate balances, skip paid ones, and show an Interactive Review Card first. Nothing is sent until you confirm!\n\n"
    "💬 **4. Send a Custom WhatsApp Message**\n"
    "   • `send whatsapp to +918445685268: Hello, your invoice is ready`\n"
    "   • `whatsapp to INV-101: Friendly payment reminder`\n\n"
    "💳 **5. Update Invoices & Payments**\n"
    "   • `paid INV-101` (marks invoice as fully paid)\n"
    "   • `paid INV-101 2500` (records partial payment)\n"
    "   • `delete invoice INV-101` / `clear invoices`\n\n"
    "📜 **6. Audit & Monetisation**\n"
    "   • `history` — view the audit log of all emails & WhatsApp messages\n"
    "   • `pricing` — check the plans ($1 for 7-day trial, then $10/month)\n"
    "   • `load demo` — load sample invoices to test in sandbox"
)


# ------------------------------------------------------------------ message builders
def _msg(content: list) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


def text_message(text: str) -> ChatMessage:
    return _msg([TextContent(type="text", text=text)])


def review_card_message(narration: str, plan: dict, sender: str = "") -> ChatMessage:
    n = planned_sends(plan)
    mode = lambda ch: "SANDBOX" if CHANNELS[ch].mode == "sandbox" else "LIVE"  # noqa: E731
    rows = [
        {"label": "Invoices checked", "value": str(plan["checked"])},
        {"label": "Need a reminder", "value": str(n["invoices"])},
        {"label": "Emails to send", "value": f"{n['email']} ({mode('email')})"},
        {"label": "WhatsApp to send", "value": f"{n['whatsapp']} ({mode('whatsapp')})"},
        {"label": "Flagged for review", "value": str(len(plan["review"]))},
        {"label": "Disputed (paused)", "value": str(len(plan["disputed"]))},
    ]
    label = f"Send {n['invoices']} reminder{'s' if n['invoices'] != 1 else ''}"
    if payments_enabled() and SUBS.is_active(sender):
        rows.append({"label": "Subscription", "value": f"Active until {SUBS.expiry(sender):%d %b %Y}"})
    elif payments_enabled():
        tier = SUBS.next_tier(sender)
        t, fee = TIERS[tier], fet_for_tier(tier)
        rows.append({"label": "Plan", "value": f"{t['label']}: ${fmt_fee(t['usd'])} ({fmt_fee(fee)} FET, {network_name()})"})
        label = f"Pay ${fmt_fee(t['usd'])} & send {n['invoices']}"
    payload = {
        "title": "Confirm payment reminders",
        "summary_rows": rows,
        "approve_cta": {"label": label, "primary": True, "selection": {"action": "confirm_send"}},
        "reject_cta": {"label": "Cancel", "selection": {"action": "cancel_send"}},
    }
    return _msg([
        TextContent(type="text", text=narration),
        MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": "review",
            "card_payload": json.dumps(payload),
            "preferred_drawer_width_px": "540",
        }),
    ])


def dashboard_card_message(narration: str, plan: dict, store: Store) -> ChatMessage:
    n = planned_sends(plan)
    invs = store.invoices()
    total_outstanding = sum(
        max(0.0, float(r.get("total") or 0) - float(r.get("paid") or 0))
        for r in invs
        if (r.get("status") or "").lower() != "paid"
    )
    rows = [
        {"label": "Total Invoices", "value": str(len(invs))},
        {"label": "Total Outstanding", "value": inr(total_outstanding)},
        {"label": "Ready for Reminder", "value": str(n["invoices"])},
        {"label": "Paid Invoices", "value": str(plan["paid"])},
        {"label": "Flagged / Disputed", "value": str(len(plan["review"]) + len(plan["disputed"]))},
    ]
    payload = {
        "title": "Accounts Receivable Dashboard",
        "summary_rows": rows,
        "approve_cta": {"label": "⚡ Send Reminders", "primary": True, "selection": {"action": "send_reminders_cta"}},
        "reject_cta": {"label": "📜 View History", "selection": {"action": "view_history_cta"}},
    }
    return _msg([
        TextContent(type="text", text=narration),
        MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": "dashboard",
            "card_payload": json.dumps(payload),
            "preferred_drawer_width_px": "540",
        }),
    ])


def summary_card_message(narration: str, summary: dict) -> ChatMessage:
    em = summary.get("channels", {}).get("email", {})
    wa = summary.get("channels", {}).get("whatsapp", {})
    rows = [
        {"label": "Invoices Checked", "value": str(summary.get("checked", 0))},
        {"label": "Reminders Sent", "value": str(summary.get("invoices_reminded", 0))},
        {"label": "Email Dispatched", "value": f"{em.get('accepted', 0)} accepted ({em.get('mode', 'sandbox')})"},
        {"label": "WhatsApp Dispatched", "value": f"{wa.get('accepted', 0)} accepted ({wa.get('mode', 'sandbox')})"},
        {"label": "Duplicates Prevented", "value": str(em.get('duplicates_prevented', 0) + wa.get('duplicates_prevented', 0))},
    ]
    payload = {
        "title": "Dispatch Summary Report",
        "summary_rows": rows,
        "approve_cta": {"label": "📜 View History", "primary": True, "selection": {"action": "view_history_cta"}},
        "reject_cta": {"label": "📊 Refresh Dashboard", "selection": {"action": "check_status_cta"}},
    }
    return _msg([
        TextContent(type="text", text=narration),
        MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": "summary",
            "card_payload": json.dumps(payload),
            "preferred_drawer_width_px": "540",
        }),
    ])


def earnings_text() -> str:
    if not EARNINGS:
        return "No payments received yet.\n\n" + explain_pricing()
    total = sum((e["amount"] for e in EARNINGS), Decimal(0))
    lines = [f"- {fmt_fee(e['amount'])} FET ({TIERS[e['tier']]['label']}), tx {e['tx'][:12]}..." for e in EARNINGS[-8:]]
    return f"Received {fmt_fee(total)} FET from {len(EARNINGS)} verified payment(s):\n" + "\n".join(lines)


# ------------------------------------------------------------------ intent handling
def parse_selection(text: str, has_pending: bool) -> str | None:
    """Card taps arrive as JSON (direct @mention) or as prose (planner-mediated). Handle both.
    Prose like 'yes' only counts as an answer when a plan is actually waiting for approval."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data.get("action")
    except (ValueError, TypeError):
        pass
    t = (text or "").lower()
    if "send_reminders_cta" in t:
        return "send_reminders_cta"
    if "view_history_cta" in t:
        return "view_history_cta"
    if "check_status_cta" in t:
        return "check_status_cta"
    if not has_pending:
        return None
    if "cancel_send" in t or re.search(r"\b(cancel|reject|stop)\b|don't send|do not send", t):
        return "cancel_send"
    if "confirm_send" in t or re.search(r"\b(confirm|approve|yes|proceed|go ahead|send it)\b", t):
        return "confirm_send"
    return None


def handle(sender: str, text: str) -> ChatMessage:
    raw_text = (text or "").strip()
    # 1. Strip any leading @mention (e.g. @agent1... or @collectiq)
    clean_text = re.sub(r"^(@\S+\s*)+", "", raw_text).strip()
    # 2. Strip outer single/double quotes or backticks if user wrapped the whole message
    if len(clean_text) >= 2 and (
        (clean_text.startswith("'") and clean_text.endswith("'")) or
        (clean_text.startswith('"') and clean_text.endswith('"')) or
        (clean_text.startswith("`") and clean_text.endswith("`"))
    ):
        clean_text = clean_text[1:-1].strip()

    # 3. Normalize common English typos (remainder -> reminder)
    clean_text = re.sub(r"\bremainder(s)?\b", r"reminder\1", clean_text, flags=re.I)
    t = clean_text.lower()
    today = date.today()
    store = store_for(sender)

    # 4. Check for interactive card selection or tap: confirm_send / cancel_send / CTAs
    action = parse_selection(text, sender in PENDING)
    if action == "view_history_cta":
        return text_message(format_history(store.recent_log(12)))

    if action == "check_status_cta":
        if not store.invoices():
            return text_message("You haven't added any invoices yet.\n\n" + HOW_TO_ADD)
        plan = build_plan(store, today, POLICY)
        return dashboard_card_message(format_plan(plan), plan, store)

    if action == "send_reminders_cta":
        if not store.invoices():
            return text_message("You haven't added any invoices yet.\n\n" + HOW_TO_ADD + "\n\nOr say 'load demo' to try sample data.")
        plan = build_plan(store, today, POLICY)
        n = planned_sends(plan)
        if n["invoices"] == 0:
            return text_message(
                format_plan(plan) + "\n\n"
                "Nothing needs a reminder right now.\n"
                "• Invoices already sent today are protected by cooldown to prevent duplicate spam (say 'send reminders force' to re-send anyway, or 'clear history').\n"
                "• Invoices due in the future are held until due (say 'include due soon' to send advance reminders)."
            )
        PENDING[sender] = plan
        narration = format_plan(plan) + "\n\nNothing has been sent yet. Please review and confirm."
        if payments_enabled() and not SUBS.is_active(sender):
            tier_info = TIERS[SUBS.next_tier(sender)]
            narration += f"\n\nReviewing is free. Sending needs the {tier_info['label']} (${fmt_fee(tier_info['usd'])} for {tier_info['days']} days), paid after you confirm."
        return review_card_message(narration, plan, sender)

    if action in ("confirm_send", "cancel_send"):
        plan = PENDING.pop(sender, None)
        if plan is None:
            return text_message("There's nothing waiting for approval. Say 'send reminders' and I'll prepare a review.")
        if action == "cancel_send":
            return text_message("Cancelled. Nothing was sent.")
        if payments_enabled() and not SUBS.is_active(sender):
            tier = SUBS.next_tier(sender)
            tier_info, amount = TIERS[tier], fet_for_tier(tier)
            AWAITING[sender] = {"plan": plan, "tier": tier, "amount": amount}
            TO_REQUEST[sender] = (tier, amount)
            return text_message(
                f"Approved. The {tier_info['label']} costs ${fmt_fee(tier_info['usd'])} ({fmt_fee(amount)} FET on {network_name()}) "
                f"and covers {tier_info['days']} days of reminder runs. Please approve the payment request below. "
                "I only send the reminders after the payment is verified on-chain."
            )
        results = execute_plan(store, plan, CHANNELS, POLICY, today)
        sum_dict = summarize(plan, results, CHANNELS)
        return summary_card_message(format_summary(sum_dict), sum_dict)

    # 5. Clean Reset / Wipe (clears all invoices & history completely so database is 0)
    if (re.search(r"\b(?:reset|wipe|clean\s+slate|empty\s+database|clear\s+database|reset\s+database|clear\s+invoices)\b", t)
        and not re.search(r"\b(?:demo|sample|history|log|logs|cooldown)\b", t)):
        store.clear(log=True)
        PENDING.pop(sender, None)
        AWAITING.pop(sender, None)
        return text_message(
            "All invoices and send history have been completely removed. Your database is now completely empty (0 invoices).\n\n"
            "• Say 'load demo' to load 13 sample invoices.\n"
            "• Or import your own: 'import csv d:\\invoices.csv' or paste rows into chat."
        )

    # 6. Sample demo loading (only when user explicitly mentions demo or sample)
    if re.search(r"\b(?:load\s+)?(?:demo|sample)\b", t):
        seed_demo(store, today)
        PENDING.pop(sender, None)
        AWAITING.pop(sender, None)
        return text_message(
            "Sample invoices loaded (13 invoices) and send history reset! All cooldowns cleared.\n\n"
            "Say 'status' to view your dashboard, or 'send reminders' to start."
        )

    # 7. Friendly greetings
    if re.match(r"^\s*(hi|hello|hey|greetings|good\s+(?:morning|afternoon|evening)|help|start|who\s+are\s+you|what\s+can\s+you\s+do)\s*[!.]*\s*$", t):
        return text_message(HELP)

    # 7. File / URL-based import (csv, excel, pdf, google sheet)
    file_target = detect_import_intent(clean_text) or detect_import_intent(raw_text)
    if file_target is not None:
        PENDING.pop(sender, None)
        rep_ = import_from_file(store, file_target)
        import_msg = format_import_report(rep_, len(store.invoices()))

        # Check if user also asked to send reminders in the same message:
        # e.g. "import csv d:\invoice.csv and send remainder to all"
        if re.search(r"\b(remind|reminder|send|notify|follow\s*up)\b", t) and not any(w in t for w in ("how to", "how do")):
            plan = build_plan(store, today, POLICY)
            n = planned_sends(plan)
            if n["invoices"] > 0:
                PENDING[sender] = plan
                narration = f"{import_msg}\n\n---\n\n{format_plan(plan)}\n\nPlease review the payment reminders above and confirm to send."
                return review_card_message(narration, plan, sender)

        return text_message(import_msg)

    # 8. Pasted invoices
    rep_ = import_invoices(store, clean_text)
    if rep_ is not None:
        PENDING.pop(sender, None)  # invoice data changed, so any earlier review is stale
        return text_message(format_import_report(rep_, len(store.invoices())))

    # 8. Check if this is a BATCH REMINDER intent
    # Matches natural English such as:
    #   "send remainder to everyone through whatsapp and email (whose email available)"
    #   "send reminders to all"
    #   "send payment reminders"
    #   "remind everyone"
    #   "check all invoices and send reminders"
    is_batch_reminder = bool(
        re.search(r"\b(everyone|all|everybody)\b", t) and re.search(r"\b(remind|reminder|send|notify|follow\s*up)\b", t) or
        re.search(r"\b(?:send\s+(?:payment\s+)?reminders?|check\s+.*and\s+send\s+reminders?|remind\s+(?:everyone|all|customers?))\b", t) or
        (re.search(r"\b(remind|reminder)\b", t) and re.search(r"\b(send|dispatch|email|whatsapp)\b", t))
    )

    # 9. Custom WhatsApp message (single recipient ONLY)
    # Must NOT be a batch reminder to all/everyone
    if not is_batch_reminder:
        wa_match = re.search(
            r"^(?:please\s+)?(?:send\s+(?:a\s+)?(?:custom\s+)?whatsapp(?:\s+message)?\s+to\s+|whatsapp\s+(?:message\s+)?to\s+|message\s+)"
            r"([^\s:,]+)"
            r"(?:\s+on\s+whatsapp)?[\s:,]+(.+)$",
            clean_text, re.I | re.S,
        )
        if not wa_match:
            wa_match = re.search(
                r"^(?:please\s+)?send\s+(?:a\s+)?(?:custom\s+)?message\s+to\s+([^\s:,]+)\s+on\s+whatsapp[\s:,]+(.+)$",
                clean_text, re.I | re.S,
            )

        STOP_TARGETS = {"and", "to", "all", "everyone", "everybody", "email", "emails", "whatsapp", "invoices", "them", "people", "customers", "whose", "the", "a", "an", "is", "are"}
        if wa_match:
            raw_target = wa_match.group(1).strip().strip("'\"")
            custom_body = wa_match.group(2).strip()

            if raw_target.lower() not in STOP_TARGETS and custom_body:
                phone = clean_phone(raw_target)
                recipient_name = raw_target
                inv_id, inv_num = None, None

                # If not a raw phone number, look up invoice by number or customer name
                if not phone:
                    for inv in store.invoices():
                        if (inv.get("invoice_number") or "").strip().lower() == raw_target.lower() or \
                           (inv.get("customer_name") or "").strip().lower().startswith(raw_target.lower()):
                            if inv.get("whatsapp"):
                                phone = clean_phone(inv["whatsapp"])
                                recipient_name = f"{inv['customer_name']} ({inv['invoice_number']})"
                                inv_id = inv.get("invoice_id")
                                inv_num = inv.get("invoice_number")
                                break

                if phone:
                    wa_channel = CHANNELS.get("whatsapp")
                    if wa_channel is None:
                        return text_message("WhatsApp channel is not available.")

                    res = wa_channel.send(phone, None, custom_body)

                    # Audit log the custom send
                    store.log(
                        invoice_id=inv_id or f"custom-{phone}",
                        invoice_number=inv_num or "CUSTOM",
                        customer_name=recipient_name,
                        channel="whatsapp",
                        message_type="custom",
                        ts=f"{today.isoformat()}T{datetime.now():%H:%M:%S}",
                        status=res.status,
                        mode=res.mode,
                        provider_ref=res.ref,
                        error=res.error,
                        attempts=1,
                        next_eligible=today.isoformat(),
                    )

                    if res.mode == "sandbox":
                        return text_message(
                            f"📝 [SANDBOX] Custom WhatsApp message recorded for +{phone} ({recipient_name}):\n"
                            f"\"{custom_body}\"\n\n"
                            f"Reference: {res.ref}\n"
                            "(To send real live messages to recipient phones, set Twilio credentials in .env)"
                        )
                    elif res.status == "accepted":
                        return text_message(f"✅ WhatsApp message sent to +{phone} ({recipient_name}).\nReference: {res.ref}")
                    else:
                        return text_message(f"❌ Failed to send WhatsApp message to +{phone}.\nStatus: {res.status}\nError: {res.error}")

    # 10. "how to add / import"
    if re.search(r"\b(how\s+(?:do|can)\s+i\s+(?:add|give|upload|import)|add\s+invoices?|how\s+to\s+(?:add|import))\b", t):
        return text_message(HOW_TO_ADD)

    # 12. Delete invoice
    m = re.search(r"\b(?:delete|remove)\s+invoice\s+(\S+)", clean_text, re.I)
    if m:
        PENDING.pop(sender, None)
        n = store.delete_invoice(m.group(1).strip(".,"))
        return text_message(f"Deleted {m.group(1).strip('.,')}." if n else f"I couldn't find invoice {m.group(1)}. Say 'my invoices' to see them.")

    # 13. Record payment
    m = (
        re.match(r"^\s*paid\s+(?:invoice\s+)?(\S+?)(?:\s+(?:₹|rs\.?\s*)?([\d,]+(?:\.\d+)?))?\s*$", clean_text, re.I) or
        re.match(r"^\s*mark\s+(?:invoice\s+)?(\S+)\s+(?:as\s+)?paid\s*()$", clean_text, re.I) or
        re.match(r"^\s*(?:invoice\s+)?(\S+?)\s+(?:is|has\s+been)?\s*paid(?:\s+(?:₹|rs\.?\s*)?([\d,]+(?:\.\d+)?))?\s*$", clean_text, re.I)
    )
    if m and m.group(1).lower() not in {"who", "what", "all", "everyone", "why", "how", "if", "is", "my", "our", "the"}:
        amt = float(m.group(2).replace(",", "")) if (m.lastindex and m.lastindex >= 2 and m.group(2)) else None
        res = store.record_payment(m.group(1).strip(".,"), amt)
        if res is not None:
            PENDING.pop(sender, None)
            paid, total, status = res
            return text_message(
                f"Recorded. {m.group(1).strip('.,')} is now {status.replace('_', ' ')} (paid ₹{paid:,.0f} of ₹{total:,.0f}). "
                + ("No more reminders will go out for it." if status == "paid" else "Reminders will mention the remaining balance.")
            )
        elif clean_text.lower().startswith("paid ") or clean_text.lower().startswith("mark "):
            return text_message(f"I couldn't find invoice {m.group(1)}. Say 'my invoices' to see them.")

    # 14. History / audit log
    if re.search(r"\b(?:clear|reset|wipe)\s+(?:the\s+)?(?:send\s+)?(?:history|log|logs)\b", t):
        store.db.execute("DELETE FROM reminder_log")
        store.db.commit()
        PENDING.pop(sender, None)
        return text_message("Reminder history cleared! All cooldowns have been reset, so you can re-remind all invoices now.")

    if any(w in t for w in ("history", "audit", "log", "logs")):
        return text_message(format_history(store.recent_log(12)))

    # 16. Earnings
    if re.search(r"\b(earnings?|revenue|received)\b", t):
        return text_message(earnings_text())

    # 17. Pricing / Subscription
    if re.search(r"\b(pricing|price|fees?|cost|charge|subscription|subscribe|plans?)\b", t):
        return text_message(explain_pricing(SUBS.status_line(sender) if payments_enabled() else ""))

    # 18. Policy: include/exclude due soon
    if "include due soon" in t or "exclude due soon" in t:
        POLICY.remind_due_soon = "include" in t
        return text_message(f"Policy updated: advance reminders for invoices due soon are now {'ON' if POLICY.remind_due_soon else 'OFF'}.")

    # 19. BATCH / TARGETED REMINDERS execution
    if is_batch_reminder or re.search(r"\b(remind|reminder|follow\s*up|send\s+notices?)\b", t):
        if not store.invoices():
            return text_message("You haven't added any invoices yet.\n\n" + HOW_TO_ADD + "\n\nOr say 'load demo' to try sample data.")

        # Check for specific target (invoice number or customer name)
        target = None
        target_m = re.search(r"\b(?:for|to)\s+(INV-[\w\-]+|\b[a-zA-Z0-9_\-]+\b)", clean_text, re.I)
        if target_m:
            candidate = target_m.group(1).strip()
            if candidate.lower() not in {"all", "everyone", "everybody", "them", "people", "customers", "whatsapp", "email", "both", "me", "us"}:
                target = candidate
        else:
            remind_m = re.search(r"\bremind\s+(INV-[\w\-]+|\b[a-zA-Z0-9_\-]+\b)", clean_text, re.I)
            if remind_m:
                candidate = remind_m.group(1).strip()
                if candidate.lower() not in {"all", "everyone", "everybody", "them", "people", "customers", "whatsapp", "email", "both", "me", "us"}:
                    target = candidate

        force = bool(re.search(r"\b(force|again|resend|re-send|anyway)\b", t))
        include_soon = bool(POLICY.remind_due_soon or "due soon" in t or "advance" in t)
        current_policy = Policy(
            remind_due_soon=include_soon,
            due_soon_days=POLICY.due_soon_days,
            cooldown_days=0 if force else POLICY.cooldown_days,
            max_retries=POLICY.max_retries,
            retry_delay_s=POLICY.retry_delay_s,
            high_value=POLICY.high_value,
            firm_after_days=POLICY.firm_after_days,
        )

        plan = build_plan(store, today, current_policy, target=target)
        if target and plan["checked"] == 0:
            return text_message(f"I couldn't find any invoice matching '{target}'. Say 'my invoices' to see your active invoices.")

        n = planned_sends(plan)
        if n["invoices"] == 0:
            return text_message(
                format_plan(plan) + "\n\n"
                "Nothing needs a reminder right now.\n"
                "• Invoices already sent today are protected by cooldown to prevent duplicate spam (say 'send reminders force' to re-send anyway, or 'clear history').\n"
                "• Invoices due in the future are held until due (say 'include due soon' to send advance reminders)."
            )
        PENDING[sender] = plan
        narration = format_plan(plan) + "\n\nNothing has been sent yet. Please review and confirm."
        if payments_enabled() and not SUBS.is_active(sender):
            tier_info = TIERS[SUBS.next_tier(sender)]
            narration += f"\n\nReviewing is free. Sending needs the {tier_info['label']} (${fmt_fee(tier_info['usd'])} for {tier_info['days']} days), paid after you confirm."
        return review_card_message(narration, plan, sender)

    # 20. List / show my invoices
    if re.search(r"\b(my|list|show|view|see|check|all)\b.*\binvoices?\b|\b(?:invoices?\s+(?:list|status|summary)?)\b", t):
        if not store.invoices():
            return text_message("You haven't added any invoices yet.\n\n" + HOW_TO_ADD)
        plan = build_plan(store, today, POLICY)
        return dashboard_card_message(format_invoice_list(store.invoices(), today, POLICY), plan, store)

    # 21. Status / check / overview
    if any(w in t for w in ("status", "check", "overdue", "due", "pending", "outstanding")):
        if not store.invoices():
            return text_message("You haven't added any invoices yet.\n\n" + HOW_TO_ADD)
        plan = build_plan(store, today, POLICY)
        return dashboard_card_message(format_plan(plan), plan, store)

    # Fallback to friendly HELP
    return text_message(HELP)


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    for item in msg.content:
        if isinstance(item, TextContent) and item.text.strip():
            ctx.logger.info(f"Text from {sender}: {item.text[:200]}")
            try:
                reply = handle(sender, item.text)
            except Exception as e:  # never crash the conversation
                ctx.logger.error(f"handler error: {e!r}")
                reply = text_message("Something went wrong on my side, and nothing was lost. Please try again.")
            await ctx.send(sender, reply)
            req = TO_REQUEST.pop(sender, None)
            if req is not None:  # a send was approved: now ask for the plan fee
                try:
                    await request_payment(ctx, sender, req[0], req[1], str(agent.wallet.address() if agent else ""))
                except Exception as e:
                    ctx.logger.error(f"could not send payment request: {e!r}")
                    AWAITING.pop(sender, None)
                    await ctx.send(sender, text_message("I couldn't create the payment request, so nothing was sent. Please try again."))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Ack from {sender} for {msg.acknowledged_msg_id}")


# ------------------------------------------------------------------ payment protocol (seller)
@payment_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    job = AWAITING.get(sender)
    if job is None:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="No reminder run is waiting for payment"))
        return
    if SUBS.tx_used(msg.transaction_id):
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="This transaction was already used"))
        return

    buyer = None
    if isinstance(msg.metadata, dict):
        buyer = msg.metadata.get("buyer_fet_wallet") or msg.metadata.get("buyer_fet_address")
    verified = False
    paid_enough = True
    if msg.funds and getattr(msg.funds, "amount", None) is not None:
        try:
            paid_enough = Decimal(str(msg.funds.amount)) >= (job["amount"] * Decimal("0.99"))
        except (InvalidOperation, TypeError):
            paid_enough = True  # defer to ledger verification

    if paid_enough and msg.transaction_id:
        verified = await asyncio.to_thread(
            verify_fet_payment, msg.transaction_id, job["amount"], buyer, str(agent.wallet.address() if agent else ""), ctx.logger)

    if not verified:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="Payment verification failed"))
        await ctx.send(sender, text_message("I couldn't verify the payment on-chain, so nothing was sent. You can say 'send reminders' to try again."))
        return

    AWAITING.pop(sender, None)
    plan = job["plan"]
    expires = SUBS.activate(sender, job["tier"], msg.transaction_id)
    EARNINGS.append({"tx": msg.transaction_id, "amount": job["amount"], "tier": job["tier"]})
    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    try:
        results = execute_plan(store_for(sender), plan, CHANNELS, POLICY, date.today())
        sum_dict = summarize(plan, results, CHANNELS)
        summary = format_summary(sum_dict)
        await ctx.send(sender, summary_card_message(
            f"Payment of {fmt_fee(job['amount'])} FET verified. Your {TIERS[job['tier']]['label']} is active until {expires:%d %b %Y}.\n\n{summary}",
            sum_dict
        ))
    except Exception as e:
        ctx.logger.error(f"execute after payment failed: {e!r}")
        await ctx.send(sender, text_message("Your payment was verified, but something failed while sending. The audit log has the details (say 'history')."))


@payment_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    AWAITING.pop(sender, None)
    await ctx.send(sender, text_message("Payment declined. Nothing was sent."))


if __name__ == "__main__":
    a = get_agent()
    a.run()
