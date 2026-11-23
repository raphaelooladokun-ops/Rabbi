import io
from decimal import Decimal

import pandas as pd

from core.engine import process
from core.masters import ClientConfig, ItemEntry
from core.models import FlagCode
from core.readers import get_reader


def _codes(invoices):
    return {f.code for iv in invoices for f in iv.all_flags}


def _pad(row, n=10):
    return list(row) + [None] * (n - len(row))


def make_ecvl_xlsx() -> bytes:
    H = ["Date", "Particulars", "", "", "E-Invoice Status", "IRN", "Vch Type", "Vch No.", "Debit", "Credit"]
    grid = [
        _pad(["Eurochemco Ventures Limited"]),
        _pad(["Sales Register"]),
        _pad(H),
        _pad(["", "", "", "", "", "", "Amount", "Amount"]),
        # Invoice 53151 (Kano, Awala) — two items, mixed rate; Debit = 1,575,000
        _pad(["1-Sep-26", "Awala Foam", "", "", "", "", "Sales - Kano", "53151", "1575000"]),
        _pad(["", "Toluene Sales"]),  # group row -> skipped
        _pad(["", "Toluene 180kg", "10", "107500", "1075000"]),   # standard, VAT-incl
        _pad(["", "DOP Sales"]),
        _pad(["", "DOP - 200 Kgs", "1", "500000", "500000"]),     # exempt
        # Invoice 53152 (Kano, YAFA) — single exempt item
        _pad(["1-Sep-26", "YAFA OILERS", "", "", "", "", "Sales - Kano", "53152", "500000"]),
        _pad(["", "DOP - 200 Kgs", "1", "500000", "500000"]),
        # Invoice 53151 again but a DIFFERENT branch (Lagos) -> distinct invoice
        _pad(["2-Sep-26", "Pedro Plastics", "", "", "", "", "Sales - Lagos - Ilupeju", "53151", "500000"]),
        _pad(["", "DOP - 200 Kgs", "1", "500000", "500000"]),
        # Invoice 53160 (Kano) reused by two customers -> must be flagged
        _pad(["3-Sep-26", "Cust X", "", "", "", "", "Sales - Kano", "53160", "107500"]),
        _pad(["", "Toluene 180kg", "1", "107500", "107500"]),
        _pad(["3-Sep-26", "Cust Y", "", "", "", "", "Sales - Kano", "53160", "500000"]),
        _pad(["", "DOP - 200 Kgs", "1", "500000", "500000"]),
        _pad(["", "Grand Total", "", "", "", "", "", "", "3182500"]),
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


def test_ecvl_reader_parses_parent_child_items(store):
    client = _store_with_ecvl(store)
    rows = get_reader("eurochemco").read(make_ecvl_xlsx()).rows
    # group/subtotal rows skipped; only real item lines kept
    assert {r.item_name for r in rows} == {"Toluene 180kg", "DOP - 200 Kgs"}
    assert all(r.quantity is not None and r.gross_amount is not None for r in rows)
    # branch extracted from the Vch Type
    assert {"Kano", "Lagos - Ilupeju"} <= {r.branch for r in rows}


def test_ecvl_backs_out_vat_with_quantity(store):
    client = _store_with_ecvl(store)
    rows = get_reader("eurochemco").read(make_ecvl_xlsx()).rows
    result = process(rows, client, store)
    inv = {(iv.branch, iv.invoice_number_raw): iv for iv in result.invoices}

    awala = inv[("Kano", "K53151")]
    assert len(awala.lines) == 2  # two items, group row dropped
    tol = [l for l in awala.lines if l.item_code == "ITM_T"][0]
    assert tol.tax_rate == Decimal("0.075")
    assert tol.quantity == Decimal("10")
    assert tol.line_value == Decimal("1000000")       # 1,075,000 gross / 1.075
    assert tol.unit_price == Decimal("100000")        # ex-VAT unit
    dop = [l for l in awala.lines if l.item_code == "ITM_D"][0]
    assert dop.tax_rate == Decimal("0") and dop.line_value == Decimal("500000")
    # mixed-rate invoice still reconciles to the stated VAT-inclusive total.
    assert FlagCode.INVOICE_TOTAL_MISMATCH not in _codes([awala])
    assert awala.ready is True


def test_ecvl_branch_disambiguates_and_reused_number_flagged(store):
    client = _store_with_ecvl(store)
    rows = get_reader("eurochemco").read(make_ecvl_xlsx()).rows
    result = process(rows, client, store)
    keys = {(iv.branch, iv.invoice_number_raw) for iv in result.invoices}
    # same number 53151 across two branches -> prefixed distinctly (K vs LI)
    assert ("Kano", "K53151") in keys and ("Lagos - Ilupeju", "LI53151") in keys
    # 53160 reused by two customers in the SAME branch -> flagged
    assert FlagCode.CUSTOMER_MISMATCH in _codes(result.invoices)


def test_ecvl_branch_prefix_mapping():
    from core.readers.eurochemco import _branch_prefix
    assert _branch_prefix("Kano") == "K"
    assert _branch_prefix("Lagos - Ilupeju") == "LI"
    assert _branch_prefix("Lagos-Mangoro") == "LM"
    assert _branch_prefix("Dir.Del.") == "DD"
    assert _branch_prefix("") == ""
