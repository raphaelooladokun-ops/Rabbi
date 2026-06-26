from decimal import Decimal

from core.readers import get_reader
from tests.conftest import make_friendship_xlsx, make_tally_xlsx


def test_geeta_reader_forward_fills_parent_onto_item_lines():
    result = get_reader("geeta").read(make_tally_xlsx())
    assert not result.file_errors
    # 2 item lines for INV-001 + 1 for INV-002
    assert len(result.rows) == 3

    inv1 = [r for r in result.rows if r.invoice_number_raw == "INV-001"]
    assert len(inv1) == 2
    assert all(str(r.invoice_date) == "2026-04-01" for r in inv1)
    assert all(r.customer_name == "Acme Ltd" for r in inv1)
    rice = inv1[0]
    assert rice.item_name == "Rice 50kg"
    assert rice.quantity == Decimal("10")
    assert rice.unit_price == Decimal("2000")
    assert rice.line_value == Decimal("20000")
    # Reader does not set the tax rate for Tally — engine derives it.
    assert rice.tax_rate is None
    # Stated total carried from the parent's Gross Total (VAT-inclusive).
    assert rice.invoice_stated_total == Decimal("21500")
    assert rice.stated_total_includes_vat is True


def test_geeta_cash_sale_grouped_separately():
    result = get_reader("geeta").read(make_tally_xlsx())
    inv2 = [r for r in result.rows if r.invoice_number_raw == "INV-002"]
    assert len(inv2) == 1
    assert inv2[0].customer_name == "Cash Sales"


def test_friendship_reader_flat_layout_and_tin_from_file():
    result = get_reader("friendship").read(make_friendship_xlsx())
    assert not result.file_errors
    assert len(result.rows) == 3

    f100 = [r for r in result.rows if r.invoice_number_raw == "F-100"]
    assert len(f100) == 2
    sugar = f100[0]
    assert sugar.customer_name == "Beta Foods"
    assert sugar.item_name == "Sugar 1kg"
    assert sugar.item_hsn == "1701"
    assert sugar.quantity == Decimal("10")
    assert sugar.unit_price == Decimal("1000")  # Base P, VAT-exclusive
    assert sugar.tax_rate == Decimal("0.075")  # rate straight from file
    assert sugar.customer_tin == "87654321-0001"
    assert sugar.tin_from_file is True


def test_friendship_blank_vat_is_exempt_and_na_tin_dropped():
    result = get_reader("friendship").read(make_friendship_xlsx())
    f101 = [r for r in result.rows if r.invoice_number_raw == "F-101"][0]
    assert f101.tax_rate == Decimal("0")  # blank VAT -> exempt
    assert f101.customer_tin is None  # "#N/A" treated as missing
    assert f101.tin_from_file is False
