from decimal import Decimal

from core.audit import new_run_id
from core.insights import customer_stats, records_signature, summarize_report
from tests.conftest import make_tally_xlsx


_REPORT_CSV = (
    "Date,Time,Invoice Reference Number,Invoice Number,Customer Name,Customer TIN,"
    "Currency,Taxable Amount,Tax Amount,Payable Amount,Total\n"
    "09 Aug 2026,3:06:53 PM,REF-1,INV-A,PURECHEM INDUSTRIES,01630676-0001,NGN,18000.00,1350.00,19350.00,19350.00\n"
    "06 Aug 2026,8:40:08 AM,REF-2,INV-B,PURECHEM INDUSTRIES,01630676-0001,NGN,2000.00,150.00,2150.00,2150.00\n"
    "05 Aug 2026,1:00:00 PM,REF-3,INV-C,,,NGN,500.00,0.00,500.00,500.00\n"   # blank customer -> skipped
).encode("utf-8")


def test_summarize_report_recognises_digitax_format():
    s = summarize_report(_REPORT_CSV)
    assert s["ok"] is True
    assert s["invoices"] == 2          # the blank-customer row is excluded
    assert s["customers"] == 1
    # A non-report CSV is rejected.
    assert summarize_report(b"foo,bar\n1,2\n")["ok"] is False


def test_customer_stats_from_digitax_report(store, geeta_client):
    run = new_run_id("geeta")
    store.save_artifact("geeta", run, "digitax_report", "report.csv", _REPORT_CSV, "admin")
    by_name = {s.customer_name: s for s in customer_stats(store, "geeta")}
    assert "PURECHEM INDUSTRIES" in by_name
    pc = by_name["PURECHEM INDUSTRIES"]
    assert pc.invoices == 2                       # INV-A + INV-B
    assert pc.total_ex_vat == Decimal("20000.00")  # taxable amounts summed
    assert pc.kind == "B2B"                        # TIN present in the report
    assert pc.top_invoice_number == "INV-A"        # bigger taxable amount


def test_report_reupload_does_not_double_count(store, geeta_client):
    store.save_artifact("geeta", new_run_id("geeta"), "digitax_report", "r1.csv", _REPORT_CSV, "admin")
    before = records_signature(store, "geeta")
    store.save_artifact("geeta", new_run_id("geeta"), "digitax_report", "r2.csv", _REPORT_CSV, "admin")
    assert records_signature(store, "geeta") != before
    pc = {s.customer_name: s for s in customer_stats(store, "geeta")}["PURECHEM INDUSTRIES"]
    assert pc.invoices == 2   # INV-A/INV-B counted once despite two uploads


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
