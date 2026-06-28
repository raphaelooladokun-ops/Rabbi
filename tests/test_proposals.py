from core.masters import ItemEntry
from core.proposals import (
    derive_state_code,
    extract_tin,
    fuzzy_best_item,
    next_item_codes,
    propose_items,
    propose_party,
    party_entry_from_proposal,
    run_period,
)
from datetime import date


def _items():
    return [
        ItemEntry(name="TIGER BLADE", item_code="ITM_001", hsn_code="8212.10",
                  tax_category="STANDARD_VAT", item_category="Industrial Consumable"),
        ItemEntry(name="FORMAMIDE AR GRADE", item_code="ITM_016", hsn_code="2924.19",
                  tax_category="STANDARD_VAT", item_category="Industrial Lab Consumables"),
        ItemEntry(name="WHITE NET", item_code="ITM_200", hsn_code="5608.19",
                  tax_category="STANDARD_VAT", item_category="Industrial Consumable"),
    ]


def test_fuzzy_match_picks_close_existing_item():
    match, score = fuzzy_best_item("tiger  blade", _items())
    assert match.item_code == "ITM_001"
    assert score > 0.9


def test_next_item_codes_continue_sequence_same_width():
    codes = next_item_codes(_items(), 3)
    # Highest is ITM_200 (width 3) -> 201, 202, 203.
    assert codes == ["ITM_201", "ITM_202", "ITM_203"]


def test_next_item_codes_from_empty_master():
    assert next_item_codes([], 2) == ["ITM_001", "ITM_002"]


def test_propose_items_separates_matches_from_new():
    unknown = ["TIGER BLADE ", "FORMAMIDE AR GRADE 2.5L", "BRAND NEW WIDGET"]
    props = propose_items(unknown, _items())
    by_name = {p.name: p for p in props}

    # Exact-ish dup -> confirm mapping, no new code.
    assert by_name["TIGER BLADE "].kind == "possible_match"
    assert by_name["TIGER BLADE "].match_code == "ITM_001"
    # Close wording -> mapped to the existing FORMAMIDE item.
    assert by_name["FORMAMIDE AR GRADE 2.5L"].kind == "possible_match"
    assert by_name["FORMAMIDE AR GRADE 2.5L"].match_code == "ITM_016"
    # No plausible match -> genuinely new, drafted from master patterns.
    widget = by_name["BRAND NEW WIDGET"]
    assert widget.kind == "new"
    assert widget.item_code == "ITM_201"  # continues the sequence
    assert widget.tax_category_code == "STANDARD_VAT"  # vocabulary reused
    assert widget.item_category == "Industrial Consumable"  # commonest category


def test_propose_items_one_code_per_physical_item():
    # Same item repeated -> a single proposal / single code.
    props = propose_items(["WIDGET", "widget", "WIDGET "], _items())
    new = [p for p in props if p.kind == "new"]
    assert len(new) == 1


def test_extract_tin_and_state_from_address_text():
    addr = "12, Owonikoko Street, Ikeja, Lagos. TIN: 01234567-0001"
    assert extract_tin(addr) == "01234567-0001"
    assert derive_state_code(addr) == "NG-LA"
    assert derive_state_code("no state here") == ""


def test_party_proposal_requires_tin_email_and_address_for_b2b():
    full = propose_party("Acme", tin_hint="01234567-0001", email="a@b.com",
                         address_text="1 Road, Ikeja, Lagos")
    assert full.can_be_b2b is True
    assert party_entry_from_proposal(full).status == "B2B"
    assert full.state == "NG-LA"
    assert full.local_government == "NG-LA-IKE"  # matched from the LGA reference

    # An unrecognised area is left blank — never fabricated.
    no_lga = propose_party("Acme", tin_hint="01234567-0001", email="a@b.com",
                           address_text="1 Nowhere Close, Lagos")
    assert no_lga.local_government == ""

    partial = propose_party("Beta", tin_hint="01234567-0001", address_text="1 Road, Lagos")
    assert partial.can_be_b2b is False  # no email
    assert "email" in partial.reason
    assert party_entry_from_proposal(partial).status == "B2C"


def test_never_fabricates_tin():
    p = propose_party("Gamma", address_text="No tin in here, Lagos")
    assert p.tin == ""
    assert p.can_be_b2b is False


def test_run_period_picks_common_month():
    assert run_period([date(2026, 1, 5), date(2026, 1, 20), date(2026, 2, 1)]) == "202601"
