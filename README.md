# CollectIQ — AI Accounts Receivable Agent

An intelligent, autonomous accounts-receivable agent built for **ASI:One** and **Agentverse**. You communicate in plain natural language to track invoices, calculate outstanding balances, review interactive confirmation cards, and dispatch personalized payment reminders via **WhatsApp** and **Email** — all securely logged to an ACID-compliant SQLite audit trail.

```
You (ASI:One Chat) ──► agent.py (uAgent + Chat Protocol + Interactive Cards)
                               │
                               ├──► file_intake.py (CSV / Excel / PDF / Google Sheets / Google Docs)
                               ├──► intake.py (Direct chat text parser & validator)
                               ├──► collectiq_core.py (Classification, Guardrails, Cooldown, Audit Log)
                               │      ├── SQLite Database (Invoices + reminder_log audit trail)
                               │      ├── Email: SMTP (Gmail / Custom SMTP) + Sandbox fallback
                               │      └── WhatsApp: Twilio API + Meta Cloud API + Custom Direct Messages
                               └──► payment.py (Agent Payment Protocol, $1 Trial / $10 Monthly, Ledger Verification)
```

---

## 📁 Project Architecture & Files

| File | Description |
|---|---|
| `agent.py` | The main uAgent: ASI:One Chat Protocol, review cards, intent dispatch, payment coordination |
| `collectiq_core.py` | Brain of CollectIQ: validation, classification, guardrails, SMTP email, Twilio/Cloud WhatsApp channels, SQLite store |
| `file_intake.py` | File & URL import engine: CSV, Excel (.xlsx/.xls), PDF, Google Docs & Google Sheets |
| `intake.py` | Chat-pasted invoice parser with lenient column matching, date normalizer, and validation |
| `payment.py` | Monetisation engine: Agent Payment Protocol, SQLite subscription storage, on-chain Fetch.ai ledger verification |
| `try_email.py` | Diagnostic script to test live email delivery via SMTP |
| `try_twilio.py` | Diagnostic script to test Twilio WhatsApp template messages |
| `try_twilio_custom.py` | Diagnostic script to test custom freeform WhatsApp messages |
| `test_collectiq_core.py`| Automated test suite for core logic, phone cleaning, and Twilio channels |
| `test_file_intake.py` | Automated test suite for file/URL importers |
| `test_intake.py` | Automated test suite for chat text parser |
| `test_payment.py` | Automated test suite for payments and SQLite subscription persistence |
| `AGENT_README.md` | Manifest description displayed publicly on Agentverse & ASI:One |
| `.env.example` | Template for environment variables and secrets |
| `requirements.txt` | Complete list of Python dependencies |

---

## ⚡ 1. Setup & Installation

Requires Python 3.10+.

```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Mac/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in your `AGENT_SEED` (any secure private passphrase). Keep this constant so your agent address remains stable on Agentverse.

---

## 🧪 2. Run Automated Tests

Run the full test suite (65 tests covering classification, intake, files, WhatsApp, interactive cards, and payments):

```bash
pytest
```

---

## 🚀 3. Run the Agent

```bash
python agent.py
```

- Each ASI:One chat user receives their own isolated private database in `data/` (`<hash>.db`).
- Subscriptions and global state are managed in `collectiq.db` via SQLite.
- If live credentials are not present, channels safely operate in **SANDBOX** mode, saving copies of dispatched messages to `outbox.jsonl`.

---

## 🌐 4. Connect to ASI:One via Agentverse Mailbox

1. Start `python agent.py`. An **Agent Inspector** URL will appear in the console. Open it in your browser.
2. Click **Connect**, select **Mailbox**, and log in to Agentverse.
3. Once the mailbox access token is saved, your agent is accessible worldwide without requiring a public IP or port forwarding.
4. Search for your agent in **ASI:One** and start chatting!

---

## 📥 5. Importing Invoices (Multiple Data Formats)

CollectIQ supports 5 different intake mechanisms:

### A. CSV Files
```
import csv d:\invoices.csv
# or: upload csv /path/to/invoices.csv
```

### B. Excel Workbooks (.xlsx / .xls)
```
import excel d:\data.xlsx
```

### C. Public Google Sheets & Google Docs
Simply paste the Google Sheets or Google Docs share link:
```
import google sheet https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit
import google doc https://docs.google.com/document/d/1xY.../edit
```
*(Ensure the sheet/doc has "Anyone with the link can view" permissions enabled).*

### D. PDF Invoices & Statements
```
import pdf d:\invoices.pdf
```
Extracts structured tables or text lines into verified records.

### E. Direct Chat Paste
Paste one invoice per line directly into the chat:
```
INV-201, Rahul Sharma, rahul@gmail.com, 9876543210, 5000, 0, 2026-11-05
INV-202, Amit Traders, amit@gmail.com, , 10000, 4000, 12/11/2026, partially_paid
```

---

## 💬 6. WhatsApp Reminders & Custom Messages

CollectIQ supports both **automated batch reminders** and **custom on-demand WhatsApp messages**:

### Automated Invoice Reminders
Run the standard command:
```
Check all invoices and send payment reminders
```
CollectIQ classifies all invoices, shows an Interactive Review Card, and dispatches reminders with Indian currency formatting (`₹6,000`), due dates, and customer details.

### Custom WhatsApp Messaging
Send direct personal messages on the fly:
```
send whatsapp to +918445685268: Hello! Please confirm if you received our recent receipt.
```
Or target an invoice number or customer name directly:
```
whatsapp to INV-101: Hey Rahul, we noticed your payment is pending. Let us know if you need any help!
message Rahul on whatsapp: Thanks for clearing your dues!
```

Both Twilio Programmable Messaging and Meta WhatsApp Cloud API are natively supported.

---

## 💳 7. Subscription Model & Payment Protocol

CollectIQ incorporates an Agent Payment Protocol monetisation workflow:

| Plan | Price | Duration | What is Included |
|---|---|---|---|
| **7-Day Trial** | **$1** | 7 Days | Full reminder runs (trial pricing for new users) |
| **Monthly Plan** | **$10** | 30 Days | Unlimited invoice processing & reminders |

- **Reviewing is always 100% free**: Checking status, classifying invoices, reviewing plans, and auditing history never charges anything.
- **On-Chain Verification**: The user approves via ASI:One's payment interface. CollectIQ queries the Fetch.ai ledger to verify the transfer before dispatching messages.
- **SQLite Database Persistence**: Active subscriptions and used transaction hashes are saved in SQLite (`collectiq.db`), preventing double-spend and surviving restarts.

Turn payments on in `.env`:
```ini
PAYMENTS=on
FET_USE_TESTNET=true
FET_PER_USD=0.1
```

---

## 🛡️ 8. Safety & Guardrails (Why CollectIQ Wins)

1. **Anti-Hallucination Guard**: Messages are strictly verified by Python logic before sending. If any generated message drops the invoice number, alters the outstanding amount, or invents illegal penalty fees, UPI links, or legal threats, it is rejected and replaced by a verified template.
2. **Partial Payment Math**: If a customer owes ₹10,000 and paid ₹4,000, CollectIQ reminds them of ₹6,000 — never the full ₹10,000.
3. **Audit Trail**: Every send attempt is recorded in SQLite with timestamp, provider reference ID, status, and retry counts (`history` command).
4. **Duplicate Protection**: A configurable cooldown window ensures customers are not spammed with duplicate reminders.
5. **Human-in-the-Loop**: Interactive ASI:One cards ensure the business owner always gives the final sign-off.

---

## 🏆 Hackathon Checklist Alignment

- [x] **Agent registered on Agentverse**: `agent.py` runs with `mailbox=True`.
- [x] **Agent Chat Protocol (ACP)**: Implements `chat_protocol_spec` with acknowledgements and published manifest.
- [x] **ASI:One Interactive Cards**:
  - 📊 **Dashboard Card**: Visual breakdown of total outstanding, overdue, and paid invoices with 1-click CTA triggers (`status` / `my invoices`).
  - 📝 **Review Card**: Human-in-the-loop review of all pending reminders before any email or WhatsApp is dispatched (`approve_cta` / `reject_cta`).
  - 📈 **Execution Summary Card**: Interactive post-dispatch delivery report with provider stats and audit shortcuts.
- [x] **Agent Payment Protocol**: $1 trial / $10 monthly plans verified on Fetch.ai blockchain ledger.
- [x] **Real External Services**: Real SMTP email and Twilio WhatsApp messaging with sandbox fallback.
- [x] **Multi-Source Data Intake**: CSV, Excel, PDF, Google Sheets, Google Docs, and chat text.
- [x] **Targeted & Batch Reminders**: Remind everyone at once or target a single customer/invoice (`remind Amit` or `send reminder for INV-103`).
- [x] **ACID Database**: SQLite database for invoices, user data, audit logs, and subscriptions.
- [x] **Production Grade**: Comprehensive test suite (65 tests passing).
