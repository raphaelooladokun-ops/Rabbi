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


def test_csv_one_row_per_item_line_with_fixed_constants(store, geeta_client):
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    text = write_csv(result.invoices)
    records = list(csv.DictReader(io.StringIO(text)))
    # 3 item lines across the two ready invoices.
    assert len(records) == 3
    for rec in records:
        assert rec["invoice_type_code"] == "388"
        assert rec["document_currency_code"] == "NGN"
        assert rec["invoice_kind"] in {"B2B", "B2C"}
        assert rec["item_code"].startswith("ITM_")

    b2b = [r for r in records if r["invoice_kind"] == "B2B"]
    assert b2b and all(r["party_tin"] == "12345678-0001" for r in b2b)
    b2c = [r for r in records if r["invoice_kind"] == "B2C"]
    assert b2c and all(r["party_tin"] == "" for r in b2c)


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
