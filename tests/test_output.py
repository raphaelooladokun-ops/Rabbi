import csv
import io

from core.config import DIGITAX_COLUMNS
from core.engine import process
from core.output import build_exception_rows, write_csv, write_exceptions_csv
from core.readers import get_reader
from tests.conftest import make_tally_xlsx


def test_csv_header_matches_digitax_exactly(store, geeta_client):
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    text = write_csv(result.invoices)
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert tuple(header) == DIGITAX_COLUMNS


def test_optional_columns_keep_the_optional_suffix():
    # Digitax bulk upload rejects optional columns without the "(optional)"
    # suffix. Lock the exact header so this can't regress.
    assert "party_tin(optional)" in DIGITAX_COLUMNS
    assert "issue_time(optional)" in DIGITAX_COLUMNS
    assert "discount_rate(optional)" in DIGITAX_COLUMNS
    assert "payment_terms_note(optional)" in DIGITAX_COLUMNS
    # Required columns must NOT carry the suffix.
    for required in ("trader_invoice_number", "invoice_type_code", "invoice_date",
                     "issue_date", "document_currency_code", "item_code",
                     "quantity", "unit_price", "tax_rate", "invoice_kind"):
        assert required in DIGITAX_COLUMNS
        assert f"{required}(optional)" not in DIGITAX_COLUMNS


def test_csv_one_row_per_item_line_with_fixed_constants(store, geeta_client):
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    text = write_csv(result.invoices)
    records = list(csv.DictReader(io.StringIO(text)))
    # 3 item lines across the two ready invoices.
    assert len(records) == 3
    for rec in records:
        assert rec["invoice_type_code"] == "381"  # Commercial Invoice per Digitax reference
        assert rec["document_currency_code"] == "NGN"
        assert rec["invoice_kind"] in {"B2B", "B2C"}
        assert rec["item_code"].startswith("ITM_")

    b2b = [r for r in records if r["invoice_kind"] == "B2B"]
    assert b2b and all(r["party_tin(optional)"] == "12345678-0001" for r in b2b)
    b2c = [r for r in records if r["invoice_kind"] == "B2C"]
    assert b2c and all(r["party_tin(optional)"] == "" for r in b2c)


def test_only_ready_invoices_written(store, geeta_client):
    # Remove an item so INV-001 has an unresolved line -> excluded.
    from core.masters import ItemEntry
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    text = write_csv(result.invoices, only_ready=True)
    records = list(csv.DictReader(io.StringIO(text)))
    nums = {r["trader_invoice_number"] for r in records}
    assert "INV-001" not in nums  # has unknown 'Bread' line -> blocked
    assert "INV-002" in nums


def test_output_dates_are_processing_date_not_source(store, geeta_client):
    import re
    from datetime import date
    rows = get_reader("geeta").read(make_tally_xlsx()).rows  # source dates are 2026-04
    result = process(rows, geeta_client, store)
    records = list(csv.DictReader(io.StringIO(write_csv(result.invoices, doc_date=date(2026, 2, 2)))))
    assert records, "expected at least one row"
    for rec in records:
        # No backdating: every row carries the processing date, YYYY-MM-DD.
        assert rec["invoice_date"] == "2026-02-02"
        assert rec["issue_date"] == "2026-02-02"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["invoice_date"])
    # Default (no doc_date) stamps today.
    today = date.today().isoformat()
    recs2 = list(csv.DictReader(io.StringIO(write_csv(result.invoices))))
    assert all(r["invoice_date"] == today for r in recs2)


def test_tax_point_date_is_the_actual_source_invoice_date(store, geeta_client):
    from datetime import date
    rows = get_reader("geeta").read(make_tally_xlsx()).rows  # INV-001 = 01/04/2026
    result = process(rows, geeta_client, store)
    records = list(csv.DictReader(io.StringIO(
        write_csv(result.invoices, only_ready=False, doc_date=date(2026, 8, 4)))))
    by_num = {r["trader_invoice_number"]: r for r in records}
    inv1 = by_num["INV-001"]
    # invoice/issue dates = upload day (no backdating); tax point = real date.
    assert inv1["invoice_date"] == "2026-08-04"
    assert inv1["issue_date"] == "2026-08-04"
    assert inv1["tax_point_date(optional)"] == "2026-04-01"
    assert by_num["INV-002"]["tax_point_date(optional)"] == "2026-04-02"


def test_all_export_includes_flagged_with_blank_item_code(store, geeta_client):
    from core.masters import ItemEntry
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    all_records = list(csv.DictReader(io.StringIO(write_csv(result.invoices, only_ready=False))))
    nums = {r["trader_invoice_number"] for r in all_records}
    assert "INV-001" in nums  # flagged invoice still present in the ALL export
    bread = [r for r in all_records if r["trader_invoice_number"] == "INV-001"
             and r["item_code"] == ""]
    assert bread  # unknown 'Bread' line exported with a blank item_code to fill in


def test_unit_price_rounded_to_two_dp_and_value_preserved(store, geeta_client):
    # A back-computed price with a long tail (the ITM_1446 case) must emit a
    # <=2dp unit_price, and the residual is absorbed into the quantity so the
    # line's pre-VAT value is preserved.
    from decimal import Decimal
    from core.masters import ItemEntry
    from core.models import LineRow
    store.seed_items("geeta", [ItemEntry(name="Bulk", item_code="ITM_B", tax_category="STANDARD_VAT")])
    qty = Decimal("1469180")
    price = Decimal("41.21432874120258")
    value = price * qty
    row = LineRow(source_row=1, invoice_number_raw="INV-R", customer_name="Cash Sales",
                  item_name="Bulk", quantity=qty, unit_price=price, line_value=value,
                  tax_rate=Decimal("0.075"))
    result = process([row], geeta_client, store)
    rec = list(csv.DictReader(io.StringIO(write_csv(result.invoices))))[0]
    up = rec["unit_price"]
    assert "." not in up or len(up.split(".")[1]) <= 2  # Digitax 2dp rule
    emitted = Decimal(rec["unit_price"]) * Decimal(rec["quantity"])
    assert abs(emitted - value) <= Decimal("1")  # residual absorbed to within a kobo


def test_clean_unit_price_and_quantity_pass_through_unchanged(store, geeta_client):
    from decimal import Decimal
    from core.masters import ItemEntry
    from core.models import LineRow
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])
    row = LineRow(source_row=1, invoice_number_raw="INV-C", customer_name="Cash Sales",
                  item_name="Rice 50kg", quantity=Decimal("10"), unit_price=Decimal("172"),
                  line_value=Decimal("1720"), tax_rate=Decimal("0.075"))
    result = process([row], geeta_client, store)
    rec = list(csv.DictReader(io.StringIO(write_csv(result.invoices))))[0]
    assert rec["quantity"] == "10"      # untouched — nothing to absorb
    assert rec["unit_price"] == "172"


def test_exceptions_report_lists_unknown_item_with_source_rows(store, geeta_client):
    from core.masters import ItemEntry
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    report = build_exception_rows(result)
    item_rows = [r for r in report if r["issue"] == "item_not_found"]
    assert item_rows
    assert item_rows[0]["item"] == "Bread"
    assert item_rows[0]["severity"] == "ERROR"
    assert item_rows[0]["source_rows"]  # points at the original sheet row
    # CSV form has the documented header.
    header = next(csv.reader(io.StringIO(write_exceptions_csv(result))))
    assert header[:3] == ["severity", "issue", "invoice_number"]
