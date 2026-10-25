"""Send a custom WhatsApp message via Twilio or inspect in sandbox.

Usage:
    python try_twilio_custom.py +919876543210 "Hello, this is a test message from CollectIQ!"
"""
import sys
from collectiq_core import build_channels, clean_phone, load_env

load_env()

if len(sys.argv) < 3:
    print("Usage: python try_twilio_custom.py <phone_number> <message>")
    print("Example: python try_twilio_custom.py +918445685268 'Hello from CollectIQ!'")
    sys.exit(1)

raw_phone = sys.argv[1]
message = sys.argv[2]

phone = clean_phone(raw_phone)
if not phone:
    sys.exit(f"Invalid phone number: '{raw_phone}'. Use a format like +918445685268.")

channels = build_channels()
wa = channels.get("whatsapp")

print(f"WhatsApp channel mode: {wa.mode.upper()}")
print(f"Recipient: +{phone}")
print(f"Message: {message}\n")

res = wa.send(phone, None, message)
print("Status   :", res.status)
print("Reference:", res.ref)
if res.error:
    print("Error    :", res.error)
if res.mode == "sandbox":
    print("\n(Running in SANDBOX mode. To send real messages, ensure TWILIO credentials are configured in .env)")
else:
    print("\nMessage dispatched through live provider!")
