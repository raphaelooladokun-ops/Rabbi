import csv
import io

from core.config import DIGITAX_COLUMNS
from core.engine import process
from core.output import write_csv
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
