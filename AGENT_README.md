# CollectIQ - AI Accounts Receivable Agent for ASI:One & Agentverse

CollectIQ is an autonomous accounts-receivable agent designed for **ASI:One** and **Agentverse**. It tracks outstanding payments, reads invoices from multiple sources (files, databases, spreadsheets, or chat), computes exact balances, flags discrepancies, and dispatches personalized reminders via **WhatsApp** and **Email** with human-in-the-loop review cards.

## Key Features

- **Multi-Format Data Intake**:
  - Paste directly into chat (comma/tab-separated)
  - CSV files (`import csv invoices.csv`)
  - Excel workbooks (`import excel invoices.xlsx`)
  - PDF invoices & statements (`import pdf bill.pdf`)
  - Public Google Sheets & Google Docs URLs (`import google sheet <url>`)
- **Automated Intelligence & Guardrails**:
  - Automatically identifies due, overdue, and upcoming invoices
  - Excludes paid invoices and pauses disputed accounts
  - Calculates partial payments: reminds only the *outstanding balance*, not the original total
  - Strict anti-hallucination guard: rejects any AI message that drops dates/amounts or invents penalty fees/links
  - Cooldown window and database audit log prevent duplicate messages
- **Multi-Channel Dispatch**:
  - **WhatsApp**: Twilio Programmable Messaging & Meta WhatsApp Cloud API with template & custom message support
  - **Email**: Real SMTP delivery (e.g. Gmail) with TLS and Message-ID tracking
  - **Direct Custom Messaging**: Send on-demand messages like `send whatsapp to +919876543210: Your invoice is due`
  - Safe sandbox fallback when credentials are not configured
- **Monetisation (Agent Payment Protocol)**:
  - Reviewing invoices and checking status is **100% free**
  - Sending reminders requires a subscription: **$1 for a 7-day trial, then $10/month**
  - Paid via FET with on-chain verification on Fetch.ai ledger
- **ASI:One Interactive Cards**:
  - **Dashboard Card**: Visual breakdown of accounts receivable with 1-click CTA buttons (`status` / `my invoices`)
  - **Review Card**: Human-in-the-loop confirmation before any email or WhatsApp message is sent (`approve_cta` / `reject_cta`)
  - **Execution Summary Card**: Post-dispatch report with real-time provider metrics and audit log links

## Try Saying in ASI:One

| Goal | What to Say |
|---|---|
| **Add / Import Invoices** | `"add invoices"` or `"import csv data.csv"` or `"import google sheet https://..."` |
| **View Invoices** | `"my invoices"` or `"status"` |
| **Check & Remind** | `"Check all invoices and send payment reminders"` |
| **Custom WhatsApp** | `"send whatsapp to +918445685268: Payment received, thank you!"` or `"whatsapp to INV-101: Please clear your balance"` |
| **Record Payment** | `"paid INV-201"` or `"paid INV-201 3000"` |
| **Audit Trail** | `"history"` |
| **Pricing & Subscription** | `"pricing"` or `"earnings"` |
| **Advance Reminders** | `"include due soon"` or `"exclude due soon"` |
| **Sample Demo** | `"load demo"` |

*Human approval is always requested before sending. Without live email/WhatsApp credentials, CollectIQ runs in a clearly marked SANDBOX mode.*
