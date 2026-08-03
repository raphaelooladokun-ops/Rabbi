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


def test_process_is_repeatable_and_reflects_master_changes(store, geeta_client):
    # The UI re-runs process() on the SAME row objects every interaction.
    # It must not accumulate stale flags, and must pick up master additions.
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])

    r1 = process(rows, geeta_client, store)
    process(rows, geeta_client, store)  # extra reruns must not change anything
    r2 = process(rows, geeta_client, store)
    assert len(r2.blocking_invoices) == len(r1.blocking_invoices)  # no accumulation
    assert FlagCode.ITEM_NOT_FOUND in _codes(r2.invoices)  # 'Bread' still unknown

    # Add the missing item, re-run on the SAME rows -> it must clear.
    store.upsert_item("geeta", ItemEntry(name="Bread", item_code="ITM_002", tax_category="EXEMPT"))
    r3 = process(rows, geeta_client, store)
    assert FlagCode.ITEM_NOT_FOUND not in _codes(r3.invoices)
    assert len(r3.blocking_invoices) == 0


def test_unknown_item_flags_error(store, geeta_client):
    store.seed_items("geeta", [ItemEntry(name="Bread", item_code="ITM_002", tax_category="EXEMPT")])
    rows = get_reader("geeta").read(make_tally_xlsx()).rows
    result = process(rows, geeta_client, store)
    assert FlagCode.ITEM_NOT_FOUND in _codes(result.invoices)
    # The invoice with the unknown Rice line must not be exportable.
    inv1 = [iv for iv in result.invoices if iv.invoice_number_raw == "INV-001"][0]
    assert inv1.ready is False


def test_friendship_party_tin_comes_from_master_not_sales_file(store):
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

    # Beta Foods is NOT in the parties master yet, even though the ledger has a
    # TIN -> treated as B2C and flagged (the file TIN is only a hint).
    result = process(rows, client, store)
    by_num = {iv.invoice_number_raw: iv for iv in result.invoices}
    assert by_num["F-100"].invoice_kind == "B2C"
    assert by_num["F-100"].party_tin is None
    assert FlagCode.CUSTOMER_NOT_FOUND in _codes([by_num["F-100"]])
    # The ledger TIN is preserved as a hint to pre-fill the new-party proposal.
    assert any(ln.customer_tin_hint == "87654321-0001" for ln in by_num["F-100"].lines)

    # Add the customer to the master as B2B -> output TIN now comes from there.
    store.upsert_party("friendship", PartyEntry(name="Beta Foods", tin="87654321-0001", status="B2B"))
    result2 = process(rows, client, store)
    f100 = [iv for iv in result2.invoices if iv.invoice_number_raw == "F-100"][0]
    assert f100.invoice_kind == "B2B"
    assert f100.party_tin == "87654321-0001"
    assert FlagCode.INVOICE_TOTAL_MISMATCH not in _codes([f100])
    assert f100.stated_total == Decimal("20000")  # 10000 + 10000 pre-VAT


def test_junk_tin_not_applicable_never_reaches_invoice(store):
    # A bogus "NOT APPLICABLE" TIN in the master must not produce a B2B
    # invoice or emit the junk value.
    store.seed_items("friendship", [ItemEntry(name="Sugar 1kg", item_code="ITM_S", tax_category="STANDARD_VAT")])
    store.upsert_party("friendship", PartyEntry(name="Beta Foods", tin="NOT APPLICABLE", status="B2B"))
    client = store.get_client("friendship")
    rows = get_reader("friendship").read(make_friendship_xlsx()).rows
    result = process(rows, client, store)
    f100 = [iv for iv in result.invoices if iv.invoice_number_raw == "F-100"][0]
    assert f100.invoice_kind == "B2C"
    assert f100.party_tin is None


def test_friendship_ignores_not_applicable_tin_hint():
    # "#N/A" is already blank; ensure a non-digit TIN isn't kept as a hint.
    from core.parsing import looks_like_tin
    assert looks_like_tin("NOT APPLICABLE") is False
    assert looks_like_tin("01058206-0001") is True
    assert looks_like_tin("1234567") is False  # only 7 digits


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


def test_duplicate_same_item_same_price_lines_merged(store, geeta_client):
    # Digitax rejects a repeated item_code; same item at the same price on one
    # invoice must be folded into a single line (summing quantity/value).
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])
    rows = [
        LineRow(source_row=1, invoice_number_raw="INV-D", customer_name="Cash Sales",
                item_name="Rice 50kg", quantity=Decimal("10"), unit_price=Decimal("100"),
                line_value=Decimal("1000"), tax_rate=Decimal("0.075")),
        LineRow(source_row=2, invoice_number_raw="INV-D", customer_name="Cash Sales",
                item_name="Rice 50kg", quantity=Decimal("5"), unit_price=Decimal("100"),
                line_value=Decimal("500"), tax_rate=Decimal("0.075")),
    ]
    result = process(rows, geeta_client, store)
    iv = result.invoices[0]
    assert len(iv.lines) == 1
    assert iv.lines[0].quantity == Decimal("15")
    assert iv.lines[0].line_value == Decimal("1500")
    assert FlagCode.DUPLICATE_ITEM not in _codes(result.invoices)
    assert iv.ready is True


def test_duplicate_same_item_different_price_flagged_not_merged(store, geeta_client):
    # Same item at two different prices (the ITM_922 case) cannot be safely
    # merged; it is flagged as an error for the operator, not guessed.
    store.seed_items("geeta", [ItemEntry(name="Rice 50kg", item_code="ITM_001", tax_category="STANDARD_VAT")])
    rows = [
        LineRow(source_row=1, invoice_number_raw="INV-E", customer_name="Cash Sales",
                item_name="Rice 50kg", quantity=Decimal("10"), unit_price=Decimal("151.8"),
                line_value=Decimal("1518"), tax_rate=Decimal("0.075")),
        LineRow(source_row=2, invoice_number_raw="INV-E", customer_name="Cash Sales",
                item_name="Rice 50kg", quantity=Decimal("5"), unit_price=Decimal("120"),
                line_value=Decimal("600"), tax_rate=Decimal("0.075")),
    ]
    result = process(rows, geeta_client, store)
    iv = result.invoices[0]
    assert len(iv.lines) == 2  # left intact
    assert FlagCode.DUPLICATE_ITEM in _codes(result.invoices)
    assert iv.ready is False  # blocked for review


def test_shared_item_code_across_different_names_flagged(store, geeta_client):
    # Two different products wrongly share one item_code in the master (the
    # "2kg" vs "400gm" case). They must never be merged, and the flag carries
    # the colliding names so the admin split tool can mint a fresh code.
    store.seed_items("geeta", [
        ItemEntry(name="Infinity 2kg Custard Jar - White", item_code="ITM_1477", tax_category="STANDARD_VAT"),
        ItemEntry(name="Infinity 400gm Custard Jar - White", item_code="ITM_1477", tax_category="STANDARD_VAT"),
    ])
    rows = [
        LineRow(source_row=11, invoice_number_raw="3071", customer_name="Cash Sales",
                item_name="Infinity 2kg Custard Jar - White", quantity=Decimal("1"),
                unit_price=Decimal("100"), line_value=Decimal("100"), tax_rate=Decimal("0.075")),
        LineRow(source_row=12, invoice_number_raw="3071", customer_name="Cash Sales",
                item_name="Infinity 400gm Custard Jar - White", quantity=Decimal("2"),
                unit_price=Decimal("50"), line_value=Decimal("100"), tax_rate=Decimal("0.075")),
    ]
    result = process(rows, geeta_client, store)
    iv = result.invoices[0]
    assert FlagCode.SHARED_ITEM_CODE in _codes(result.invoices)
    assert len(iv.lines) == 2      # not merged — they are different products
    assert iv.ready is False
    f = next(fl for fl in iv.all_flags if fl.code == FlagCode.SHARED_ITEM_CODE)
    assert f.context["shared_code"] == "ITM_1477"
    assert set(f.context["colliding_names"]) == {
        "Infinity 2kg Custard Jar - White", "Infinity 400gm Custard Jar - White"}


def test_total_mismatch_flagged(store, geeta_client):
    row = LineRow(
        source_row=1, invoice_number_raw="INV-9", customer_name="Cash Sales",
        item_name="Rice 50kg", quantity=Decimal("1"), unit_price=Decimal("100"),
        line_value=Decimal("100"), invoice_stated_total=Decimal("999"),
        stated_total_includes_vat=True,
    )
    result = process([row], geeta_client, store)
    assert FlagCode.INVOICE_TOTAL_MISMATCH in _codes(result.invoices)
