"""The database-backed store must behave identically to the file store and
survive a 'restart' (reconnecting to the same database)."""
from decimal import Decimal

import pytest

from core.bootstrap import ensure_default_clients
from core.masters import ItemEntry, PartyEntry

sqlalchemy = pytest.importorskip("sqlalchemy")
from core.masters_sql import SqlMasterStore  # noqa: E402


def _url(tmp_path):
    return f"sqlite:///{tmp_path / 'masters.db'}"


def test_seed_lookup_and_persist_across_reconnect(tmp_path):
    url = _url(tmp_path)
    s = SqlMasterStore(url)
    ensure_default_clients(s)
    assert {c.id for c in s.list_clients()} == {"geeta", "friendship", "goldcoin"}

    s.seed_items("geeta", [
        ItemEntry(name="Rice 50kg", item_code="ITM_001", hsn_code="1006", tax_category="STANDARD_VAT"),
        ItemEntry(name="Bread", item_code="ITM_002", hsn_code="1905", tax_category="EXEMPT"),
    ])
    s.upsert_party("geeta", PartyEntry(name="Acme Ltd", tin="12345678-0001", status="B2B"))
    c = s.get_client("geeta")
    c.tin_suffix_rule = "-0001"
    s.save_client(c)

    # Reconnect to the same database (simulates a host restart).
    s2 = SqlMasterStore(url)
    assert len(s2.list_items("geeta")) == 2
    assert s2.lookup_item("geeta", name="rice 50kg").item_code == "ITM_001"  # case-insensitive
    assert s2.lookup_item("geeta", hsn="1905").item_code == "ITM_002"  # by HSN
    assert s2.lookup_party("geeta", "ACME LTD").tin == "12345678-0001"
    assert s2.get_client("geeta").tin_suffix_rule == "-0001"
    assert s2.tax_rate_for("STANDARD_VAT") == Decimal("0.075")


def test_seed_replaces_existing(tmp_path):
    s = SqlMasterStore(_url(tmp_path))
    s.seed_items("x", [ItemEntry(name="A", item_code="1")])
    s.seed_items("x", [ItemEntry(name="B", item_code="2")])
    names = {e.name for e in s.list_items("x")}
    assert names == {"B"}  # second seed fully replaces the first
