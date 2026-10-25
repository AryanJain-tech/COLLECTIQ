"""Send ONE test email with your .env settings, so you know real email works before the demo.

Usage:  python try_email.py you@gmail.com
"""
import sys

from collectiq_core import build_channels, load_env

load_env()

if len(sys.argv) != 2:
    sys.exit("Usage: python try_email.py er.aryanjain1706@gmail.com")

email = build_channels()["email"]
if email.mode != "live":
    sys.exit("Email is still in SANDBOX mode. Fill SMTP_HOST, SMTP_USER, SMTP_PASS and SMTP_FROM in .env first.")

res = email.send(sys.argv[1], "CollectIQ test email",
                 "If you can read this, CollectIQ can send real email.")
print("status:", res.status)
print("reference:", res.ref)
print("error:", res.error)
