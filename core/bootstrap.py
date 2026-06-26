"""First-run setup: register the three initial clients if none exist yet.

The item and party masters themselves are seeded by the operator (importing
their existing helper columns/sheets) through the UI, so this only creates the
client records and reasonable defaults.
"""
from __future__ import annotations

from .masters import ClientConfig, MasterStore

DEFAULT_CLIENTS = [
    ClientConfig(
        id="geeta",
        name="Geeta",
        reader="geeta",
        b2b_expected=True,
        notes="Tally Sales Register (.xlsx). Resolve customers via parties master.",
    ),
    ClientConfig(
        id="friendship",
        name="Friendship Co",
        reader="friendship",
        b2b_expected=True,
        notes="SALES LEDGER sheet (.xlsx). TIN and VAT rate come from the file.",
    ),
    ClientConfig(
        id="bag",
        name="Bag Client",
        reader="bag",
        b2b_expected=False,
        notes="Tally Sales Register (.xls). Branch-coded voucher numbers; B2C unless a "
        "B2B customer is added. Confirm with the operator whether any B2B customers exist.",
    ),
]


def ensure_default_clients(store: MasterStore) -> None:
    if store.list_clients():
        return
    for client in DEFAULT_CLIENTS:
        store.save_client(client)
