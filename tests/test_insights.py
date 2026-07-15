from decimal import Decimal

from core.audit import new_run_id
from core.insights import customer_stats, records_signature
from tests.conftest import make_tally_xlsx


def test_customer_stats_from_stored_uploads(store, geeta_client):
    run = new_run_id("geeta")
    store.save_artifact("geeta", run, "uploaded_raw", "sales.xlsx", make_tally_xlsx(), "admin")
    # A generated CSV in the same run must be ignored (it has no customer names).
    store.save_artifact("geeta", run, "invoices_csv", "out.csv", b"a,b\n1,2\n", "admin")

    by_name = {s.customer_name: s for s in customer_stats(store)}
    assert set(by_name) == {"Acme Ltd", "Cash Sales"}

    acme = by_name["Acme Ltd"]
    assert acme.invoices == 1
    assert acme.total_ex_vat == Decimal("20000")  # pre-VAT line sum
    assert acme.kind == "B2B"                      # from the parties master
    assert acme.top_invoice_number == "INV-001"    # its biggest invoice
    assert acme.top_invoice_value == Decimal("20000")

    cash = by_name["Cash Sales"]
    assert cash.invoices == 1
    assert cash.total_ex_vat == Decimal("1000")
    assert cash.kind == "B2C"
    assert cash.top_invoice_number == "INV-002"


def test_reupload_of_same_period_does_not_double_count(store, geeta_client):
    # The same invoices uploaded twice (a re-run) must not inflate the figures.
    r1 = new_run_id("geeta")
    store.save_artifact("geeta", r1, "uploaded_raw", "sales.xlsx", make_tally_xlsx(), "admin")
    before = records_signature(store)
    r2 = new_run_id("geeta")
    store.save_artifact("geeta", r2, "uploaded_raw", "sales_again.xlsx", make_tally_xlsx(), "admin")
    assert records_signature(store) != before  # more records -> new fingerprint

    acme = {s.customer_name: s for s in customer_stats(store)}["Acme Ltd"]
    assert acme.invoices == 1                     # INV-001 counted once, not twice
    assert acme.total_ex_vat == Decimal("20000")


def _tally_two_acme_invoices() -> bytes:
    import io
    import pandas as pd
    grid = [
        ["Geeta Stores Ltd", None, None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None, None],
        ["Date", "Particulars", "Voucher Type", "Voucher No.", "VAT No.", "Quantity", "Rate", "Value", "Gross Total"],
        ["01/04/2026", "Acme Ltd", "Sales", "INV-001", "12345678-0001", None, None, "20000", "21500"],
        [None, "Rice 50kg", None, None, None, "10", "2000", "20000", None],
        ["03/04/2026", "Acme Ltd", "Sales", "INV-003", "12345678-0001", None, None, "50000", "53750"],
        [None, "Rice 50kg", None, None, None, "25", "2000", "50000", None],
    ]
    buf = io.BytesIO()
    pd.DataFrame(grid).to_excel(buf, index=False, header=False)
    buf.seek(0)
    return buf.getvalue()


def test_biggest_invoice_is_the_max_value_one(store, geeta_client):
    run = new_run_id("geeta")
    store.save_artifact("geeta", run, "uploaded_raw", "sales.xlsx", _tally_two_acme_invoices(), "admin")
    acme = {s.customer_name: s for s in customer_stats(store)}["Acme Ltd"]
    assert acme.invoices == 2
    assert acme.total_ex_vat == Decimal("70000")       # 20000 + 50000
    assert acme.top_invoice_number == "INV-003"        # the bigger one
    assert acme.top_invoice_value == Decimal("50000")


def test_client_filter_scopes_results(store, geeta_client):
    run = new_run_id("geeta")
    store.save_artifact("geeta", run, "uploaded_raw", "sales.xlsx", make_tally_xlsx(), "admin")
    assert customer_stats(store, "geeta")           # geeta has data
    assert customer_stats(store, "friendship") == []  # nothing stored for friendship
