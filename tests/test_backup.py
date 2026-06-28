import json
from decimal import Decimal

from core.backup import export_all, export_json, import_all
from core.masters import ClientConfig, ItemEntry, MasterStore, PartyEntry


def _seed(store):
    store.save_client(ClientConfig(id="geeta", name="Geeta", reader="geeta", b2b_expected=True,
                                   tin_suffix_rule="-0001", invoice_type_code="381"))
    store.seed_items("geeta", [
        ItemEntry(name="Rice 50kg", item_code="ITM_001", hsn_code="1006",
                  tax_category="STANDARD_VAT", item_category="Food", description="rice"),
    ])
    store.seed_parties("geeta", [
        PartyEntry(name="Acme Ltd", tin="12345678-0001", status="B2B",
                   email_address="a@b.com", state="NG-LA", local_government="NG-LA-IKE"),
    ])
    store.save_tax_rates({"STANDARD_VAT": Decimal("0.075"), "EXEMPTED": Decimal("0")})


def test_export_import_round_trip(tmp_path):
    src = MasterStore(tmp_path / "src")
    _seed(src)
    blob = json.loads(export_json(src))

    dst = MasterStore(tmp_path / "dst")
    summary = import_all(dst, blob)
    assert summary == {"clients": 1, "items": 1, "parties": 1}

    c = dst.get_client("geeta")
    assert c.invoice_type_code == "381" and c.tin_suffix_rule == "-0001"
    item = dst.lookup_item("geeta", name="Rice 50kg")
    assert item.item_code == "ITM_001" and item.item_category == "Food"
    party = dst.lookup_party("geeta", "Acme Ltd")
    assert party.tin == "12345678-0001" and party.local_government == "NG-LA-IKE"
    assert dst.tax_rate_for("STANDARD_VAT") == Decimal("0.075")


def test_export_structure(tmp_path):
    s = MasterStore(tmp_path / "s")
    _seed(s)
    data = export_all(s)
    assert data["version"] == 1
    assert "exported_at" in data
    assert set(data) >= {"clients", "items", "parties", "tax_rates"}
    assert data["items"]["geeta"][0]["item_code"] == "ITM_001"


def test_import_tolerates_unknown_and_missing_fields(tmp_path):
    s = MasterStore(tmp_path / "s")
    # An item record from a hypothetical newer/older version.
    data = {
        "clients": [{"id": "x", "name": "X", "reader": "geeta"}],
        "items": {"x": [{"name": "Widget", "item_code": "ITM_9", "future_field": "ignored"}]},
        "parties": {}, "tax_rates": {},
    }
    import_all(s, data)
    assert s.lookup_item("x", name="Widget").item_code == "ITM_9"
