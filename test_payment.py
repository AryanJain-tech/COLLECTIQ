from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import payment

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("PAYMENTS", "FET_PER_USD", "FET_USE_TESTNET"):
        monkeypatch.delenv(k, raising=False)


def test_payments_are_off_by_default_and_switchable(monkeypatch):
    assert payment.payments_enabled() is False
    monkeypatch.setenv("PAYMENTS", "on")
    assert payment.payments_enabled() is True


def test_plan_prices_and_periods():
    assert payment.TIERS["trial"]["usd"] == Decimal("1") and payment.TIERS["trial"]["days"] == 7
    assert payment.TIERS["monthly"]["usd"] == Decimal("10") and payment.TIERS["monthly"]["days"] == 30


def test_usd_converts_to_fet_with_configurable_rate(monkeypatch):
    assert payment.fmt_fee(payment.fet_for_tier("trial")) == "4"
    assert payment.fmt_fee(payment.fet_for_tier("monthly")) == "40"
    monkeypatch.setenv("FET_PER_USD", "0.1")
    assert payment.fmt_fee(payment.fet_for_tier("trial")) == "0.1"


def test_trial_first_then_monthly_and_expiry(tmp_path):
    subs = payment.Subscriptions(str(tmp_path / "s.json"))
    assert subs.next_tier("a") == "trial" and not subs.is_active("a", NOW)
    exp = subs.activate("a", "trial", "tx1", NOW)
    assert exp == NOW + timedelta(days=7)
    assert subs.is_active("a", NOW + timedelta(days=6)) and not subs.is_active("a", NOW + timedelta(days=8))
    assert subs.next_tier("a") == "monthly"  # the $1 trial is one-time
    assert subs.activate("a", "monthly", "tx2", NOW + timedelta(days=8)) == NOW + timedelta(days=38)


def test_early_renewal_extends_instead_of_wasting_days(tmp_path):
    subs = payment.Subscriptions(str(tmp_path / "s.json"))
    subs.activate("a", "trial", "tx1", NOW)
    assert subs.activate("a", "monthly", "tx2", NOW + timedelta(days=2)) == NOW + timedelta(days=37)


def test_subscriptions_and_used_transactions_survive_restart(tmp_path):
    path = str(tmp_path / "s.json")
    payment.Subscriptions(path).activate("a", "trial", "tx1", NOW)
    again = payment.Subscriptions(path)
    assert again.tx_used("tx1") and not again.tx_used("tx2")
    assert again.expiry("a") == NOW + timedelta(days=7) and again.next_tier("a") == "monthly"


def test_sqlite_subscriptions_database_persistence(tmp_path):
    db_path = str(tmp_path / "subs.db")
    subs = payment.Subscriptions(db_path)
    assert subs.is_db is True
    assert subs.next_tier("user1") == "trial"
    assert not subs.is_active("user1", NOW)

    # 1. Activate trial: $1 for 7 days
    exp = subs.activate("user1", "trial", "tx_trial_100", NOW)
    assert exp == NOW + timedelta(days=7)
    assert subs.is_active("user1", NOW + timedelta(days=6))
    assert not subs.is_active("user1", NOW + timedelta(days=8))
    assert subs.tx_used("tx_trial_100") is True
    assert subs.tx_used("tx_unused") is False

    # After trial is used, next tier is monthly: $10/month
    assert subs.next_tier("user1") == "monthly"

    # 2. Database persistence across new instance / restart
    reloaded = payment.Subscriptions(db_path)
    assert reloaded.is_active("user1", NOW + timedelta(days=6))
    assert reloaded.next_tier("user1") == "monthly"
    assert reloaded.tx_used("tx_trial_100") is True

    # 3. Monthly subscription activation: 30 days
    exp2 = reloaded.activate("user1", "monthly", "tx_monthly_200", NOW + timedelta(days=8))
    assert exp2 == NOW + timedelta(days=38)


def test_pricing_text_states_both_plans(monkeypatch):
    text = payment.explain_pricing()
    assert "$1 for the first 7 days" in text and "$10 per month" in text and "OFF" in text
    monkeypatch.setenv("PAYMENTS", "on")
    assert "ON" in payment.explain_pricing()


def test_verification_failure_never_raises_and_returns_false():
    class Log:
        def error(self, *a, **k):
            pass

        info = error

    assert payment.verify_fet_payment("deadbeef", Decimal("1"), "fetch1buyer", "fetch1seller", Log()) is False
