from datetime import date

import pytest

from collectiq_core import Policy, Store, build_plan, planned_sends
from intake import extract_rows, format_invoice_list, import_invoices

TODAY = date(2026, 10, 24)
FAST = Policy(retry_delay_s=0)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "u.db"))


def test_plain_rows_without_header_are_imported(store):
    rep = import_invoices(store, "INV-201, Rahul Sharma, rahul@gmail.com, 9876543210, 5000, 0, 2026-10-20\n"
                                 "INV-202, Amit Traders, amit@gmail.com, , \u20b910,000, 4,000, 12/11/2026, partially_paid")
    # the second row has a comma inside the amounts: that is a malformed row, reported and not stored
    assert rep["added"] == ["INV-201"] and len(rep["errors"]) == 1


def test_header_row_with_aliases_and_dates(store):
    text = ("invoice, customer, email, whatsapp, amount, paid, due, status\n"
            "INV-1, A, a@x.com, 9876543210, 5000, 0, 5 Nov 2026, pending\n"
            "INV-2, B, b@x.com, , 10000, 4000, 12/11/2026,\n")
    rep = import_invoices(store, text)
    assert rep["added"] == ["INV-1", "INV-2"] and not rep["errors"]
    rows = {r["invoice_number"]: r for r in store.invoices()}
    assert rows["INV-1"]["due_date"] == "2026-11-05" and rows["INV-1"]["status"] == "unpaid"
    assert rows["INV-2"]["due_date"] == "2026-11-12" and rows["INV-2"]["status"] == "partially_paid"


def test_bad_rows_are_reported_not_stored(store):
    rep = import_invoices(store, "INV-3, C, c@x.com, , abc, 0, 2026-11-01\nINV-4, D, d@x.com, , 500, 900, 2026-11-01\n"
                                 "INV-5, E, e@x.com, , 500, 0, someday")
    assert rep["added"] == [] and len(rep["errors"]) == 3 and store.invoices() == []


def test_same_number_updates_instead_of_duplicating(store):
    import_invoices(store, "INV-9, F, f@x.com, , 1000, 0, 2026-11-01")
    rep = import_invoices(store, "INV-9, F, f@x.com, , 1500, 0, 2026-11-01")
    assert rep["updated"] == ["INV-9"] and len(store.invoices()) == 1 and store.invoices()[0]["total"] == 1500


def test_invalid_contacts_warn_and_consent_no_is_respected(store):
    rep = import_invoices(store, "INV-6, G, not-an-email, 123, 800, 0, 2026-11-01, , no")
    assert rep["added"] == ["INV-6"] and len(rep["warnings"]) == 3
    r = store.invoices()[0]
    assert r["email"] is None and r["whatsapp"] is None and r["email_opt_in"] == 0


def test_normal_chat_and_card_taps_are_not_treated_as_invoices(store):
    for text in ("Check all invoices and send payment reminders", '{"action": "confirm_send"}', "status", "hello, how are you, today?"):
        assert extract_rows(text) is None


def test_imported_invoices_flow_into_the_reminder_plan(store):
    import_invoices(store, "INV-10, H, h@x.com, 9876543210, 5000, 0, 2026-10-20\nINV-11, I, i@x.com, , 800, 0, 2026-12-30")
    plan = build_plan(store, TODAY, FAST)
    assert plan["checked"] == 2 and planned_sends(plan) == {"email": 1, "whatsapp": 1, "invoices": 1}
    assert "overdue 4 days" in format_invoice_list(store.invoices(), TODAY)


def test_payment_and_delete_helpers(store):
    import_invoices(store, "INV-12, J, j@x.com, , 1000, 0, 2026-10-01")
    assert store.record_payment("inv-12", 400) == (400.0, 1000.0, "partially_paid")
    assert store.record_payment("INV-12") == (1000.0, 1000.0, "paid")
    assert store.record_payment("NOPE") is None
    assert store.delete_invoice("INV-12") == 1 and store.invoices() == []
