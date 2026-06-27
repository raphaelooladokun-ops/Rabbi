from datetime import date
from decimal import Decimal

from core.engine import process
from core.masters import ItemEntry, PartyEntry
from core.models import FlagCode, LineRow
from core.readers import get_reader
from tests.conftest import make_friendship_xlsx, make_tally_xlsx


def _codes(invoices):
    return {f.code for iv in invoices for f in iv.all_flags}


def test_geeta_end_to_end_resolves_codes_rates_and_kind(store, geeta_client):
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)

    by_num = {iv.invoice_number_raw: iv for iv in result.invoices}
    inv1 = by_num["INV-001"]
    # Acme Ltd is a B2B party in the master -> TIN attached.
    assert inv1.invoice_kind == "B2B"
    assert inv1.party_tin == "12345678-0001"
    rice = [l for l in inv1.lines if l.item_name == "Rice 50kg"][0]
    assert rice.item_code == "ITM_001"
    assert rice.tax_rate == Decimal("0.075")  # from STANDARD_VAT category
    bread = [l for l in inv1.lines if l.item_name == "Bread"][0]
    assert bread.tax_rate == Decimal("0")  # EXEMPT

    inv2 = by_num["INV-002"]
    assert inv2.invoice_kind == "B2C"  # Cash Sales
    assert inv2.party_tin is None


def test_reconciliation_matches_pre_vat_subtotal(store, geeta_client):
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    # Pre-VAT line sum (20000 + 0) matches the pre-VAT subtotal 20000 -> no
    # mismatch. (Reconciling against the 21500 gross would be off by 7.5%.)
    assert FlagCode.INVOICE_TOTAL_MISMATCH not in _codes(result.invoices)
    inv1 = [iv for iv in result.invoices if iv.invoice_number_raw == "INV-001"][0]
    assert inv1.stated_total == Decimal("20000")
    assert inv1.stated_includes_vat is False


def test_unknown_item_flags_error(store, geeta_client):
    store.seed_items("geeta", [ItemEntry(name="Bread", item_code="ITM_002", tax_category="EXEMPT")])
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    assert FlagCode.ITEM_NOT_FOUND in _codes(result.invoices)
    # The invoice with the unknown Rice line must not be exportable.
    inv1 = [iv for iv in result.invoices if iv.invoice_number_raw == "INV-001"][0]
    assert inv1.ready is False


def test_friendship_uses_file_tin_and_flags_na_customer(store):
    store.seed_items(
        "friendship",
        [
            ItemEntry(name="Sugar 1kg", item_code="ITM_S", hsn_code="1701", tax_category="STANDARD_VAT"),
            ItemEntry(name="Salt 500g", item_code="ITM_T", hsn_code="2501", tax_category="STANDARD_VAT"),
            ItemEntry(name="Bread", item_code="ITM_B", hsn_code="1905", tax_category="EXEMPT"),
        ],
    )
    client = store.get_client("friendship")
    rows = get_reader("friendship").read(make_friendship_xlsx()).rows
    result = process(rows, client, store)
    by_num = {iv.invoice_number_raw: iv for iv in result.invoices}
    assert by_num["F-100"].invoice_kind == "B2B"
    assert by_num["F-100"].party_tin == "87654321-0001"
    # F-101 had #N/A TIN and customer not in master -> B2C + flagged.
    assert by_num["F-101"].invoice_kind == "B2C"
    assert FlagCode.CUSTOMER_NOT_FOUND in _codes([by_num["F-101"]])
    # Pre-VAT line totals are summed across the multi-line invoice and
    # reconciled VAT-exclusive, so no false mismatch and no 7.5% drift.
    assert FlagCode.INVOICE_TOTAL_MISMATCH not in _codes([by_num["F-100"]])
    assert by_num["F-100"].stated_total == Decimal("20000")  # 10000 + 10000 pre-VAT
    assert by_num["F-100"].stated_includes_vat is False


def test_item_resolves_by_hsn_when_name_differs(store):
    store.seed_items(
        "friendship",
        [ItemEntry(name="Granulated Sugar", item_code="ITM_S", hsn_code="1701", tax_category="STANDARD_VAT")],
    )
    client = store.get_client("friendship")
    row = LineRow(source_row=1, invoice_number_raw="X", item_name="Sugar 1kg", item_hsn="1701",
                  quantity=Decimal("1"), unit_price=Decimal("100"), line_value=Decimal("100"),
                  tax_rate=Decimal("0.075"))
    result = process([row], client, store)
    assert result.invoices[0].lines[0].item_code == "ITM_S"


def test_total_mismatch_flagged(store, geeta_client):
    row = LineRow(
        source_row=1, invoice_number_raw="INV-9", customer_name="Cash Sales",
        item_name="Rice 50kg", quantity=Decimal("1"), unit_price=Decimal("100"),
        line_value=Decimal("100"), invoice_stated_total=Decimal("999"),
        stated_total_includes_vat=True,
    )
    result = process([row], geeta_client, store)
    assert FlagCode.INVOICE_TOTAL_MISMATCH in _codes(result.invoices)
