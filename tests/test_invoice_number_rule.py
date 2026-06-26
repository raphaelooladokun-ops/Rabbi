from decimal import Decimal

from core.engine import process
from core.masters import ClientConfig, ItemEntry, MasterStore
from core.models import FlagCode, LineRow


def _client(tmp_path):
    s = MasterStore(tmp_path / "c")
    s.save_client(ClientConfig(id="x", name="X", reader="geeta", b2b_expected=False))
    s.seed_items("x", [ItemEntry(name="A", item_code="ITM_A", tax_category="STANDARD_VAT")])
    return s, s.get_client("x")


def _line(num, branch=None):
    return LineRow(
        source_row=1, invoice_number_raw=num, branch=branch, customer_name="Cash",
        item_name="A", quantity=Decimal("1"), unit_price=Decimal("1"), line_value=Decimal("1"),
    )


def test_short_number_passes_through(tmp_path):
    s, c = _client(tmp_path)
    result = process([_line("INV-001")], c, s)
    assert result.invoices[0].trader_invoice_number == "INV-001"


def test_long_number_trimmed_to_30(tmp_path):
    s, c = _client(tmp_path)
    num = "GC/26/K/INV/0000000000000000001-EXTRA"  # > 30 chars
    assert len(num) > 30
    result = process([_line(num)], c, s)
    iv = result.invoices[0]
    assert iv.trader_invoice_number == num[:30]
    assert len(iv.trader_invoice_number) == 30
    assert FlagCode.INVOICE_NUMBER_OVERFLOW not in {f.code for f in iv.all_flags}


def test_unsafe_trim_collision_goes_to_review(tmp_path):
    s, c = _client(tmp_path)
    # Two distinct long numbers that share the same first 30 chars.
    a = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123-AAA"
    b = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123-BBB"
    assert a[:30] == b[:30]
    result = process([_line(a), _line(b)], c, s)
    for iv in result.invoices:
        assert iv.trader_invoice_number is None
        assert FlagCode.INVOICE_NUMBER_OVERFLOW in {f.code for f in iv.all_flags}
        assert iv.ready is False


def test_trim_collides_with_existing_short_number(tmp_path):
    s, c = _client(tmp_path)
    short = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"  # exactly 30 chars, valid as-is
    long = short + "-TAIL"  # trims to the same 30 chars
    result = process([_line(short), _line(long)], c, s)
    by_num = {iv.invoice_number_raw: iv for iv in result.invoices}
    assert by_num[short].trader_invoice_number == short
    assert by_num[long].trader_invoice_number is None
    assert FlagCode.INVOICE_NUMBER_OVERFLOW in {f.code for f in by_num[long].all_flags}


def test_branch_keeps_identical_numbers_separate(tmp_path):
    s, c = _client(tmp_path)
    # Same number, different branch -> two distinct invoices.
    result = process([_line("INV/1", branch="KETU"), _line("INV/1", branch="SAGAMU")], c, s)
    assert len(result.invoices) == 2
