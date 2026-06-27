import io
from decimal import Decimal

import pandas as pd
import pytest

from core.masters import ClientConfig, ItemEntry, MasterStore, PartyEntry


@pytest.fixture
def store(tmp_path):
    """A MasterStore backed by a temp dir, seeded with the three clients."""
    s = MasterStore(tmp_path / "clients")
    s.save_client(ClientConfig(id="geeta", name="Geeta", reader="geeta", b2b_expected=True))
    s.save_client(
        ClientConfig(id="friendship", name="Friendship Co", reader="friendship", b2b_expected=True)
    )
    s.save_client(ClientConfig(id="bag", name="Bag Client", reader="bag", b2b_expected=False))
    return s


@pytest.fixture
def geeta_client(store):
    store.seed_items(
        "geeta",
        [
            ItemEntry(name="Rice 50kg", item_code="ITM_001", hsn_code="1006", tax_category="STANDARD_VAT"),
            ItemEntry(name="Bread", item_code="ITM_002", hsn_code="1905", tax_category="EXEMPT"),
        ],
    )
    store.seed_parties(
        "geeta",
        [PartyEntry(name="Acme Ltd", tin="12345678-0001", status="B2B")],
    )
    return store.get_client("geeta")


def make_tally_xlsx() -> bytes:
    """Build a minimal Tally-style sales register with a title block,
    header row, two invoices, parent + child item lines."""
    grid = [
        ["Geeta Stores Ltd", None, None, None, None, None, None, None],
        ["123 Market Rd, Lagos", None, None, None, None, None, None, None],
        ["Sales Register 1-Apr-2026 to 30-Apr-2026", None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None],
        ["Date", "Particulars", "Voucher Type", "Voucher No.", "VAT No.", "Quantity", "Rate", "Value", "Gross Total"],
        # Invoice 1 — parent then two item lines
        ["01/04/2026", "Acme Ltd", "Sales", "INV-001", "12345678-0001", None, None, None, "21500"],
        [None, "Rice 50kg", None, None, None, "10", "2000", "20000", None],
        [None, "Bread", None, None, None, "5", "0", "0", None],
        # Invoice 2 — cash sale, single item
        ["02/04/2026", "Cash Sales", "Sales", "INV-002", None, None, None, None, "1075"],
        [None, "Rice 50kg", None, None, None, "0.5", "2000", "1000", None],
    ]
    df = pd.DataFrame(grid)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, header=False)
    buf.seek(0)
    return buf.getvalue()


def make_friendship_xlsx() -> bytes:
    grid = [
        ["Friendship Co — SALES LEDGER", None, None, None, None, None, None, None, None, None, None, None, None, None],
        [
            "Del. Date", "Invoice No", "Customer Name", "Category", "Item Description",
            "HSN Code", "UOM", "Dis. Qty", "Base P", "Line Total", "VAT",
            "Unit Price", "Total Invoice Value", "TIN NO",
        ],
        # Invoice F1, two lines, B2B with TIN. "Total Invoice Value" is the
        # per-line VAT-inclusive amount (matches the real Friendship file).
        [
            "05/04/2026", "F-100", "Beta Foods", "Goods", "Sugar 1kg",
            "1701", "BAG", "10", "1000", "10000", "0.075", "1075", "10750", "87654321-0001",
        ],
        [
            "05/04/2026", "F-100", "Beta Foods", "Goods", "Salt 500g",
            "2501", "BAG", "20", "500", "10000", "0.075", "537.5", "10750", "87654321-0001",
        ],
        # Invoice F2, exempt line (blank VAT), no TIN (#N/A)
        [
            "06/04/2026", "F-101", "Gamma Stores", "Goods", "Bread",
            "1905", "PCS", "4", "250", "1000", None, "250", "1000", "#N/A",
        ],
    ]
    df = pd.DataFrame(grid)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, header=False)
    buf.seek(0)
    return buf.getvalue()
