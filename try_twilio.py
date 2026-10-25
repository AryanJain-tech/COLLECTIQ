import os
import json
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()
client = Client(os.environ["TWILIO_ACCOUNT_SID"],
                os.environ["TWILIO_AUTH_TOKEN"])

msg = client.messages.create(
    from_=os.environ["TWILIO_WHATSAPP_FROM"],
    to="whatsapp:+918445685268",
    content_sid="HXfe5ab5f00277942d4d4200328b4d403c",  # paste yours
    content_variables=json.dumps({"1": "INV-105 (Rs 5000)", "2": "30 Oct"}),
)
print(msg.sid, msg.status)