"""Invoice intake: turn what a user pastes in the chat into validated invoice records.

Accepts one invoice per line (comma, semicolon-free CSV or tab-separated, e.g. copied from Excel / Google Sheets),
with or without a header row. Nothing is guessed silently: bad rows are reported, not stored.
"""
import csv
import re
from datetime import date, datetime

from collectiq_core import Policy, classify, clean_phone, fmt_date, inr

MAX_ROWS = 200
def _norm_col(s: str) -> str:
    return re.sub(r"[\s_.\-]+", " ", str(s or "").strip().lower())


FIELDS = ["invoice_number", "customer_name", "email", "whatsapp", "total", "paid", "due_date", "status", "consent"]
ALIASES = {
    "invoice_number": (
        "invoice", "invoice number", "invoice no", "invoice no.", "invoice id", "invoice_id",
        "invoice_number", "invoice_no", "inv", "number", "id", "inv_no", "inv_id", "bill no", "bill_no"
    ),
    "customer_name": (
        "customer", "customer name", "customer_name", "name", "client", "client name",
        "client_name", "party", "party name", "party_name", "buyer"
    ),
    "email": (
        "email", "e-mail", "email address", "email_address", "mail", "customer_email", "client_email"
    ),
    "whatsapp": (
        "whatsapp", "whatsapp number", "whatsapp_number", "phone", "phone number",
        "phone_number", "mobile", "mobile number", "mobile_no", "contact", "contact number"
    ),
    "total": (
        "amount", "total", "invoice amount", "invoice_amount", "total amount", "total_amount",
        "grand total", "grand_total", "bill amount", "subtotal", "balance_due", "balance due"
    ),
    "paid": (
        "paid", "amount paid", "amount_paid", "paid amount", "paid_amount", "received", "received amount"
    ),
    "due_date": (
        "due", "due date", "due_date", "payment due", "payment_due", "pay by", "due_on"
    ),
    "status": (
        "status", "payment status", "payment_status", "inv_status", "state"
    ),
    "consent": (
        "consent", "opt in", "opt-in", "opt_in", "agreed"
    ),
}
STATUS_WORDS = {
    "unpaid": "unpaid", "pending": "unpaid", "due": "unpaid", "overdue": "unpaid", "outstanding": "unpaid", "open": "unpaid",
    "partially_paid": "partially_paid", "partially paid": "partially_paid", "partial": "partially_paid", "part paid": "partially_paid",
    "paid": "paid", "settled": "paid", "closed": "paid",
    "disputed": "disputed", "dispute": "disputed",
}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y")
DATE_LIKE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4})\b")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
LEAD = re.compile(r"^\s*(please\s+)?(add|import|upload|save|enter)\s+(these\s+|my\s+|the\s+|an?\s+|new\s+)?(invoices?|bills?)\s*[:\-]?\s*", re.I)

HOW_TO_ADD = (
    "To add invoices, paste them here, one per line (you can copy rows straight from Excel or Google Sheets). "
    "Order of columns:\n\n"
    "invoice number, customer name, email, WhatsApp number, amount, amount paid, due date, status\n\n"
    "Example:\n"
    "INV-201, Rahul Sharma, rahul@gmail.com, 9876543210, 5000, 0, 2026-11-05\n"
    "INV-202, Amit Traders, amit@gmail.com, , 10000, 4000, 12/11/2026, partially_paid\n\n"
    "- Email or WhatsApp can be left empty (keep the commas). Status is optional.\n"
    "- Dates: 2026-11-05, 05/11/2026 (day first) or 5 Nov 2026. Amounts: 5000 or ₹5,000.\n"
    "- A header row also works: invoice, customer, email, whatsapp, amount, paid, due, status.\n"
    "- Adding a number that already exists updates that invoice.\n"
    "- Only add customers who agreed to be contacted. Add a last column 'no' for anyone who has not.\n\n"
    "📁 You can also import from files:\n"
    "- 'import csv d:\\invoices.csv'\n"
    "- 'import excel d:\\invoices.xlsx'\n"
    "- 'import google sheet https://docs.google.com/spreadsheets/d/...'\n"
    "- 'import pdf d:\\invoice.pdf'\n\n"
    "Other commands: 'my invoices', 'paid INV-201', 'paid INV-201 2000', 'delete invoice INV-201', 'clear invoices'."
)


def parse_date_any(s) -> str | None:
    s = str(s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def parse_amount(s) -> float | None:
    t = re.sub(r"(?i)₹|rs\.?|inr|,|\s", "", str(s or ""))
    try:
        return float(t)
    except ValueError:
        return None


def split_line(line: str) -> list[str]:
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    try:
        return [c.strip() for c in next(csv.reader([line]))]
    except (csv.Error, StopIteration):
        return [c.strip() for c in line.split(",")]


def _header_map(cells: list[str]) -> dict:
    m: dict[str, int] = {}
    for i, c in enumerate(cells):
        raw_key = c.strip().lower()
        norm_key = _norm_col(c)
        for field, names in ALIASES.items():
            norm_names = [_norm_col(n) for n in names]
            if (raw_key in names or norm_key in norm_names) and field not in m:
                m[field] = i
                break
    return m if len(m) >= 3 else {}


def extract_rows(text: str):
    """(column mapping, list of cell lists), or None when the text does not look like invoice data."""
    text = LEAD.sub("", (text or "").strip(), count=1)
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    mapping = _header_map(split_line(lines[0]))
    if mapping:
        rows = [split_line(l) for l in lines[1:]]
        return (mapping, rows[:MAX_ROWS]) if rows else None
    rows = []
    for l in lines:
        cells = split_line(l)
        # a full row has 7+ columns; a shorter one still counts when it contains a recognisable date
        if len(cells) >= 7 or (len(cells) >= 5 and DATE_LIKE.search(l)):
            rows.append(cells)
    if not rows:
        return None
    return ({f: i for i, f in enumerate(FIELDS)}, rows[:MAX_ROWS])


def _get(cells, mapping, field) -> str:
    i = mapping.get(field)
    return cells[i].strip() if i is not None and i < len(cells) else ""


def parse_row(cells, mapping):
    """-> (record or None, errors, warnings)"""
    errs, warns = [], []
    number, name = _get(cells, mapping, "invoice_number"), _get(cells, mapping, "customer_name")
    if not number:
        errs.append("missing invoice number")
    if not name:
        errs.append("missing customer name")
    total = parse_amount(_get(cells, mapping, "total"))
    if total is None or total <= 0:
        errs.append("amount must be a positive number")
    paid_raw = _get(cells, mapping, "paid")
    paid = 0.0 if paid_raw == "" else parse_amount(paid_raw)
    if paid is None or paid < 0:
        errs.append("amount paid is not a number")
    elif total and paid > total:
        errs.append("amount paid is more than the invoice amount")
    due = parse_date_any(_get(cells, mapping, "due_date"))
    if not due:
        errs.append("due date not understood (use 2026-11-05 or 05/11/2026)")

    email = _get(cells, mapping, "email")
    if email and not EMAIL_RE.fullmatch(email):
        warns.append(f"email '{email}' looks invalid and was ignored")
        email = ""
    phone = _get(cells, mapping, "whatsapp")
    if phone and not clean_phone(phone):
        warns.append(f"WhatsApp number '{phone}' looks invalid and was ignored")
        phone = ""
    if not email and not phone:
        warns.append("no valid email or WhatsApp number, so this customer can't be reminded yet")

    status_raw = _get(cells, mapping, "status").lower()
    status = STATUS_WORDS.get(status_raw)
    if status_raw and status is None:
        errs.append(f"unknown status '{status_raw}' (use unpaid, partially_paid, paid or disputed)")
    if not errs:
        if status is None:
            status = "paid" if paid >= total else ("partially_paid" if paid > 0 else "unpaid")
        if status == "paid" and paid < total:
            paid = total
        if status == "partially_paid" and paid <= 0:
            errs.append("partially paid needs the amount paid so far")
    consent = _get(cells, mapping, "consent").lower()
    opt = 0 if consent in ("no", "n", "0", "false") else 1
    if errs:
        return None, errs, warns
    return dict(invoice_id=number, invoice_number=number, customer_name=name, email=email or None,
                whatsapp=phone or None, email_opt_in=opt, whatsapp_opt_in=opt, total=total, paid=paid,
                issue_date=None, due_date=due, status=status), [], warns


def import_invoices(store, text: str):
    """Parse and store invoices. Returns a report dict, or None when the text isn't invoice data."""
    ex = extract_rows(text)
    if ex is None:
        return None
    mapping, rows = ex
    rep = {"added": [], "updated": [], "errors": [], "warnings": []}
    for n, cells in enumerate(rows, 1):
        rec, errs, warns = parse_row(cells, mapping)
        label = cells[0] if cells and cells[0] else f"row {n}"
        rep["warnings"] += [f"{label}: {w}" for w in warns]
        if errs:
            rep["errors"].append(f"{label}: " + "; ".join(errs))
            continue
        (rep["updated"] if store.upsert_invoice(**rec) else rep["added"]).append(rec["invoice_number"])
    return rep


def format_import_report(rep: dict, total_now: int) -> str:
    lines = []
    if rep["added"] or rep["updated"]:
        bits = []
        if rep["added"]:
            bits.append(f"added {len(rep['added'])} invoice{'s' if len(rep['added']) != 1 else ''} ({', '.join(rep['added'][:8])}{'...' if len(rep['added']) > 8 else ''})")
        if rep["updated"]:
            bits.append(f"updated {len(rep['updated'])} ({', '.join(rep['updated'][:8])})")
        lines.append("Done: " + " and ".join(bits) + f". You now have {total_now} invoice{'s' if total_now != 1 else ''}.")
    else:
        lines.append("I couldn't add any invoices from that.")
    if rep["errors"]:
        lines.append("\nNot added (please fix and paste these again):\n" + "\n".join(f"- {e}" for e in rep["errors"][:10]))
    if rep["warnings"]:
        lines.append("\nHeads up:\n" + "\n".join(f"- {w}" for w in rep["warnings"][:10]))
    if rep["added"] or rep["updated"]:
        lines.append("\nSay 'my invoices' to see them, or 'send payment reminders' to review who needs a reminder.")
    else:
        lines.append("\nSay 'add invoices' to see the format.")
    return "\n".join(lines)


def format_invoice_list(rows: list[dict], today: date, policy: Policy | None = None) -> str:
    if not rows:
        return "You haven't added any invoices yet.\n\n" + HOW_TO_ADD
    policy = policy or Policy()
    out = ["| Invoice | Customer | Outstanding | Due | Status |", "|---|---|---|---|---|"]
    for r in rows[:30]:
        c = classify(r, today, policy)
        cat, d = c["category"], c["days_overdue"]
        if cat == "needs_review":
            status = "needs review: " + c["problems"][0]
        elif cat == "overdue":
            status = f"overdue {d} day{'s' if d != 1 else ''}"
        elif cat == "due_today":
            status = "due today"
        elif cat in ("due_soon", "not_due"):
            status = f"due in {-d} day{'s' if -d != 1 else ''}"
        else:
            status = cat
        due = fmt_date(c["due"]) if c["due"] else "?"
        out.append(f"| {r['invoice_number']} | {r['customer_name']} | {inr(c['outstanding']) if c['outstanding'] is not None else '?'} | {due} | {status} |")
    if len(rows) > 30:
        out.append(f"\n...and {len(rows) - 30} more.")
    return "\n".join(out)
