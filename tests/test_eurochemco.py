import io
from decimal import Decimal

import pandas as pd

from core.engine import process
from core.masters import ClientConfig, ItemEntry
from core.models import FlagCode
from core.readers import get_reader


def _codes(invoices):
    return {f.code for iv in invoices for f in iv.all_flags}


def make_ecvl_xlsx() -> bytes:
    grid = [
        ["Eurochemco Ventures Limited", None, None, None, None, None],
        ["No. 6, Industrial Avenue, Ilupeju, Lagos", None, None, None, None, None],
        ["Sales Register", None, None, None, None, None],
        ["1-Sep-26 to 30-Sep-26", None, None, None, None, None],
        ["DATE", "PARTICULARS", "PRODUCTS", "VCH NUMBER", "AMOUNT", "TIN"],
        [None, None, None, None, None, None],
        # VAT-inclusive AMOUNT, standard-rated item -> ex = 1,075,000 / 1.075 = 1,000,000
        ["1-Sep-26", "Awala Foam", "Toluene 180kg", "53149", "  1,075,000.00 ", None],
        # exempt item -> no VAT to back out
        ["1-Sep-26", "YAFA OILERS", "DOP - 200 Kgs", "53151", "  500,000.00 ", "15673177-0001"],
        # reused voucher 53149 by a DIFFERENT customer -> must be flagged
        ["2-Sep-26", "Pedro Plastics", "DOP - 200 Kgs", "53149", "  500,000.00 ", None],
        [None, "TOTAL", None, None, "  2,075,000.00 ", None],
    ]
    buf = io.BytesIO()
    pd.DataFrame(grid).to_excel(buf, index=False, header=False)
    buf.seek(0)
    return buf.getvalue()


def _store_with_ecvl(store):
    store.save_client(ClientConfig(id="ecvl", name="Eurochemco", reader="eurochemco", b2b_expected=True))
    store.seed_items("ecvl", [
        ItemEntry(name="Toluene 180kg", item_code="ITM_T", tax_category="STANDARD_VAT"),
        ItemEntry(name="DOP - 200 Kgs", item_code="ITM_D", tax_category="EXEMPTED"),
    ])
    return store.get_client("ecvl")


def test_eurochemco_backs_out_vat_from_inclusive_amount(store):
    client = _store_with_ecvl(store)
    rows = get_reader("eurochemco").read(make_ecvl_xlsx()).rows
    result = process(rows, client, store)
    by_num = {iv.invoice_number_raw: iv for iv in result.invoices}

    # Exempt line: AMOUNT passes through unchanged, rate 0.
    dop = by_num["53151"]
    ln = dop.lines[0]
    assert ln.item_code == "ITM_D"
    assert ln.tax_rate == Decimal("0")
    assert ln.quantity == Decimal("1")
    assert ln.unit_price == Decimal("500000")
    assert ln.line_value == Decimal("500000")
    assert FlagCode.INVOICE_TOTAL_MISMATCH not in _codes([dop])
    assert dop.ready is True


def test_eurochemco_standard_rate_line_is_ex_vat(store):
    client = _store_with_ecvl(store)
    rows = get_reader("eurochemco").read(make_ecvl_xlsx()).rows
    result = process(rows, client, store)
    # Voucher 53149 is reused by two customers -> flagged, not silently merged.
    assert FlagCode.CUSTOMER_MISMATCH in _codes(result.invoices)
    iv = [v for v in result.invoices if v.invoice_number_raw == "53149"][0]
    assert iv.ready is False
    toluene = [l for l in iv.lines if l.item_code == "ITM_T"][0]
    assert toluene.tax_rate == Decimal("0.075")
    # 1,075,000 VAT-inclusive -> 1,000,000 ex-VAT at 7.5%.
    assert toluene.unit_price == Decimal("1000000")
    assert toluene.line_value == Decimal("1000000")
