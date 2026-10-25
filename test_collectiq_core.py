from datetime import date, timedelta

import pytest

from collectiq_core import (
    Policy, SandboxChannel, SendResult, Store, build_plan, execute_plan, format_summary,
    guard_message, inr, planned_sends, seed_demo, summarize,
)

TODAY = date(2026, 10, 24)
FAST = Policy(retry_delay_s=0)


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    seed_demo(store, TODAY)
    out = str(tmp_path / "outbox.jsonl")
    channels = {"email": SandboxChannel("email", out), "whatsapp": SandboxChannel("whatsapp", out)}
    return store, channels


def item(plan, number):
    return next(i for i in plan["items"] if i["invoice_number"] == number)


def test_inr_uses_indian_grouping():
    assert inr(6000) == "₹6,000"
    assert inr(100000) == "₹1,00,000"
    assert inr(2500.5) == "₹2,500.50"


def test_plan_counts_for_demo_data(env):
    store, _ = env
    plan = build_plan(store, TODAY, FAST)
    assert plan["checked"] == 13
    assert plan["paid"] == 1
    assert len(plan["disputed"]) == 1
    assert len(plan["review"]) == 2  # duplicate row + unknown status
    assert len(plan["items"]) == 8
    assert planned_sends(plan) == {"email": 7, "whatsapp": 6, "invoices": 8}


def test_partial_payment_reminds_remaining_amount_only(env):
    store, _ = env
    body = item(build_plan(store, TODAY, FAST), "INV-103")["messages"]["email"]["body"]
    assert "₹6,000" in body and "₹10,000" not in body


def test_paid_disputed_duplicate_and_unknown_status_get_no_reminder(env):
    store, _ = env
    plan = build_plan(store, TODAY, FAST)
    numbers = {i["invoice_number"] for i in plan["items"]}
    assert "INV-102" not in numbers and "INV-106" not in numbers
    reasons = " ".join(" ".join(r["problems"]) for r in plan["review"])
    assert "duplicate of invoice INV-101" in reasons
    assert "unknown payment status" in reasons
    assert [i["invoice_number"] for i in plan["items"]].count("INV-101") == 1


def test_missing_contact_and_missing_consent_are_skipped_per_channel(env):
    store, _ = env
    plan = build_plan(store, TODAY, FAST)
    rohit = {c["channel"]: c for c in item(plan, "INV-107")["channels"]}
    assert rohit["email"]["action"] == "skip" and rohit["whatsapp"]["action"] == "send"
    divya = {c["channel"]: c for c in item(plan, "INV-113")["channels"]}
    assert divya["whatsapp"]["action"] == "skip" and "agreed" in divya["whatsapp"]["reason"]


def test_due_soon_only_with_policy(env):
    store, _ = env
    assert "INV-109" not in {i["invoice_number"] for i in build_plan(store, TODAY, FAST)["items"]}
    on = Policy(retry_delay_s=0, remind_due_soon=True)
    assert "INV-109" in {i["invoice_number"] for i in build_plan(store, TODAY, on)["items"]}


def test_priority_and_tone(env):
    store, _ = env
    plan = build_plan(store, TODAY, FAST)
    assert plan["items"][0]["invoice_number"] == "INV-105" and plan["items"][0]["high_value"]
    assert item(plan, "INV-104")["tone"] == "friendly"
    assert item(plan, "INV-101")["tone"] == "follow_up"
    assert item(plan, "INV-103")["tone"] == "firm"


def test_full_run_results_and_temporary_failure_is_retried_once(env):
    store, channels = env
    plan = build_plan(store, TODAY, FAST)
    results = execute_plan(store, plan, channels, FAST)
    s = summarize(plan, results, channels)
    assert s["channels"]["email"]["accepted"] == 6 and s["channels"]["email"]["failed"] == 1
    assert s["channels"]["whatsapp"]["accepted"] == 6 and s["channels"]["whatsapp"]["retried"] == 1
    sunil_wa = next(r for r in results if r["invoice_number"] == "INV-105" and r["channel"] == "whatsapp")
    assert sunil_wa["attempts"] == 2 and sunil_wa["status"] == "accepted"
    anil_email = next(r for r in results if r["invoice_number"] == "INV-110" and r["channel"] == "email")
    assert anil_email["status"] == "failed_permanent" and anil_email["attempts"] == 1  # never retried
    assert "SANDBOX" in format_summary(s)


def test_one_channel_failing_does_not_erase_the_other(env):
    store, channels = env
    plan = build_plan(store, TODAY, FAST)
    results = execute_plan(store, plan, channels, FAST)
    anil = {r["channel"]: r["status"] for r in results if r["invoice_number"] == "INV-110"}
    assert anil == {"email": "failed_permanent", "whatsapp": "accepted"}


def test_second_run_same_day_does_not_resend_accepted_messages(env):
    store, channels = env
    execute_plan(store, build_plan(store, TODAY, FAST), channels, FAST)
    again = build_plan(store, TODAY, FAST)
    assert planned_sends(again) == {"email": 1, "whatsapp": 0, "invoices": 1}  # only the rejected INV-110 email


def test_executing_the_same_plan_twice_is_blocked(env):
    store, channels = env
    plan = build_plan(store, TODAY, FAST)
    execute_plan(store, plan, channels, FAST)
    second = execute_plan(store, plan, channels, FAST)
    assert any(r["status"] == "skipped_duplicate" for r in second)
    assert not any(r["status"] == "accepted" for r in second)


def test_reminder_allowed_again_after_cooldown(env):
    store, channels = env
    execute_plan(store, build_plan(store, TODAY, FAST), channels, FAST)
    later = build_plan(store, TODAY + timedelta(days=3), FAST)
    assert planned_sends(later)["whatsapp"] >= 5


def test_unknown_outcome_is_not_retried_and_blocks_resend(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_invoice(invoice_id="9", invoice_number="INV-900", customer_name="Slow Mail",
                      email="timeout@example.com", whatsapp=None, total=1000, paid=0,
                      issue_date="2026-09-01", due_date="2026-10-01", status="unpaid")
    ch = {"email": SandboxChannel("email", str(tmp_path / "o.jsonl")), "whatsapp": SandboxChannel("whatsapp", str(tmp_path / "o.jsonl"))}
    res = execute_plan(store, build_plan(store, TODAY, FAST), ch, FAST)
    assert res[0]["status"] == "unknown" and res[0]["attempts"] == 1
    assert planned_sends(build_plan(store, TODAY, FAST))["email"] == 0


def test_channel_that_crashes_is_recorded_not_fatal(tmp_path):
    class Boom:
        mode = "sandbox"

        def send(self, *a, **k):
            raise RuntimeError("boom")

    store = Store(str(tmp_path / "t.db"))
    seed_demo(store, TODAY)
    ch = {"email": Boom(), "whatsapp": SandboxChannel("whatsapp", str(tmp_path / "o.jsonl"))}
    res = execute_plan(store, build_plan(store, TODAY, FAST), ch, FAST)
    assert all(r["status"] == "unknown" for r in res if r["channel"] == "email")
    assert any(r["status"] == "accepted" for r in res if r["channel"] == "whatsapp")


def test_inconsistent_record_is_flagged_not_messaged(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_invoice(invoice_id="1", invoice_number="INV-1", customer_name="X", email="x@example.com",
                      whatsapp=None, total=1000, paid=0, issue_date="2026-09-01", due_date="2026-10-01", status="paid")
    plan = build_plan(store, TODAY, FAST)
    assert plan["items"] == [] and "marked paid" in plan["review"][0]["problems"][0]


def test_guard_blocks_invented_links_and_lost_facts():
    due = date(2026, 10, 20)
    good = "Hello Rahul, invoice INV-101 has ₹5,000 outstanding, due 20 October 2026."
    assert guard_message(good, "INV-101", 5000, due) == []
    assert guard_message(good + " Pay at https://pay.example.com", "INV-101", 5000, due)
    assert guard_message(good.replace("₹5,000", "₹9,000"), "INV-101", 5000, due)
    assert guard_message(good + " A late fee applies.", "INV-101", 5000, due)


def test_ai_rewriter_is_used_only_when_it_passes_the_guard(env):
    store, _ = env
    ok = lambda t: "Warm regards! " + t  # noqa: E731
    sneaky = lambda t: t + " Pay now at https://evil.example"  # noqa: E731
    assert item(build_plan(store, TODAY, FAST, rewriter=ok), "INV-101")["message_source"] == "ai_polished"
    assert item(build_plan(store, TODAY, FAST, rewriter=sneaky), "INV-101")["message_source"] == "template"


def test_clean_phone_variations():
    from collectiq_core import clean_phone
    assert clean_phone("9876543210") == "919876543210"
    assert clean_phone("+91 98765-43210") == "919876543210"
    assert clean_phone("919876543210") == "919876543210"
    assert clean_phone("+17372508034") == "17372508034"
    assert clean_phone("123") is None  # invalid phone


def test_twilio_whatsapp_channel_formatting(monkeypatch):
    from collectiq_core import TwilioWhatsAppChannel, _TwilioClient

    sent_kwargs = {}

    class MockMessages:
        def create(self, **kwargs):
            sent_kwargs.update(kwargs)
            class Msg:
                sid = "SMtest123"
            return Msg()

    class MockClient:
        def __init__(self, *args, **kwargs):
            self.messages = MockMessages()

    monkeypatch.setattr("collectiq_core._TwilioClient", MockClient)

    channel = TwilioWhatsAppChannel("ACtest", "token123", "+17372508034", content_sid="HXtemplate")

    # 1. Template send
    res = channel.send("919876543210", None, "body text", meta={"params": ["Rahul", "INV-101", "₹5,000", "20 Oct"]})
    assert res.status == "accepted"
    assert res.ref == "SMtest123"
    assert sent_kwargs["to"] == "whatsapp:+919876543210"
    assert sent_kwargs["content_sid"] == "HXtemplate"

    # 2. Custom freeform send (no params in meta)
    res_custom = channel.send("+919876543210", None, "Custom message to customer")
    assert res_custom.status == "accepted"
    assert sent_kwargs["to"] == "whatsapp:+919876543210"
    assert sent_kwargs["body"] == "Custom message to customer"


def test_build_plan_with_target_filter(env):
    store, _ = env
    # Target by specific unique invoice number
    plan_inv = build_plan(store, TODAY, FAST, target="INV-103")
    assert plan_inv["checked"] == 1
    assert len(plan_inv["items"]) == 1
    assert plan_inv["items"][0]["invoice_number"] == "INV-103"

    # Target by customer name
    plan_name = build_plan(store, TODAY, FAST, target="Amit")
    assert plan_name["checked"] == 1
    assert plan_name["items"][0]["customer"] == "Amit Traders"
