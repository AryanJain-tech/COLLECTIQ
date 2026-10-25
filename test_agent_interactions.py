"""Tests for agent.py interactive cards, targeted reminders, and natural language flows."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_MAILBOX"] = "false"

import agent
from collectiq_core import Store, seed_demo
from uagents_core.contrib.protocols.chat import MetadataContent, TextContent


@pytest.fixture
def test_sender(tmp_path, monkeypatch):
    sender = "test_agent_user_123"
    db_path = str(tmp_path / "test_user.db")
    store = Store(db_path)
    monkeypatch.setattr(agent, "store_for", lambda s: store)
    monkeypatch.setattr(agent, "payments_enabled", lambda: False)
    # Clear any pending state
    agent.PENDING.pop(sender, None)
    agent.AWAITING.pop(sender, None)
    return sender, store


def test_greeting_returns_help(test_sender):
    sender, _ = test_sender
    msg = agent.handle(sender, "hello")
    assert isinstance(msg.content[0], TextContent)
    assert "👋" in msg.content[0].text
    assert "CollectIQ" in msg.content[0].text


def test_status_and_invoices_return_dashboard_card(test_sender):
    sender, store = test_sender
    seed_demo(store, agent.date.today())

    # 1. status
    msg = agent.handle(sender, "status")
    assert len(msg.content) == 2
    assert isinstance(msg.content[0], TextContent)
    meta = msg.content[1]
    assert isinstance(meta, MetadataContent)
    assert meta.metadata.get("card_kind") == "dashboard"
    payload = json.loads(meta.metadata["card_payload"])
    assert payload["title"] == "Accounts Receivable Dashboard"
    assert any(row["label"] == "Total Invoices" for row in payload["summary_rows"])

    # 2. my invoices
    msg_inv = agent.handle(sender, "my invoices")
    assert len(msg_inv.content) == 2
    assert "INV-101" in msg_inv.content[0].text
    meta_inv = msg_inv.content[1]
    assert meta_inv.metadata.get("card_kind") == "dashboard"


def test_targeted_reminder_and_summary_card(test_sender):
    sender, store = test_sender
    seed_demo(store, agent.date.today())

    # Send reminder for specific customer
    msg = agent.handle(sender, "send reminder to Amit Traders")
    assert len(msg.content) == 2
    meta = msg.content[1]
    assert meta.metadata.get("card_kind") == "review"
    assert "INV-103" in msg.content[0].text

    # Tap Approve (confirm_send)
    exec_msg = agent.handle(sender, json.dumps({"action": "confirm_send"}))
    assert len(exec_msg.content) == 2
    summary_meta = exec_msg.content[1]
    assert summary_meta.metadata.get("card_kind") == "summary"
    payload = json.loads(summary_meta.metadata["card_payload"])
    assert payload["title"] == "Dispatch Summary Report"


def test_interactive_card_cta_navigation(test_sender):
    sender, store = test_sender
    seed_demo(store, agent.date.today())

    # 1. view_history_cta
    h_msg = agent.handle(sender, json.dumps({"action": "view_history_cta"}))
    assert isinstance(h_msg.content[0], TextContent)

    # 2. check_status_cta
    s_msg = agent.handle(sender, json.dumps({"action": "check_status_cta"}))
    assert s_msg.content[1].metadata.get("card_kind") == "dashboard"

    # 3. send_reminders_cta
    r_msg = agent.handle(sender, json.dumps({"action": "send_reminders_cta"}))
    assert r_msg.content[1].metadata.get("card_kind") == "review"


def test_natural_language_paid_and_clear_history(test_sender):
    sender, store = test_sender
    seed_demo(store, agent.date.today())

    # "INV-103 paid"
    paid_msg = agent.handle(sender, "INV-103 is paid")
    assert "is now paid" in paid_msg.content[0].text

    # "clear history"
    clear_msg = agent.handle(sender, "reset history")
    assert "Reminder history cleared" in clear_msg.content[0].text
