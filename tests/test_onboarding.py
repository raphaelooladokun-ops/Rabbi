from core.masters import ItemEntry
from core.onboarding import (
    EXEMPT,
    STANDARD,
    ZERO,
    assign_item_codes,
    classify_vat,
    find_vat_column,
)


def test_find_vat_column_detects_stated_status():
    assert find_vat_column(["Item Name", "HSN", "VAT Status"]) == "VAT Status"
    assert find_vat_column(["name", "Taxable?"]) == "Taxable?"
    assert find_vat_column(["name", "tax_category_code"]) == "tax_category_code"
    # A list with no VAT/tax column -> '' (drives the "approve all VATable" mode)
    assert find_vat_column(["Item Name", "HSN", "Unit Price"]) == ""


def test_classify_vat_maps_to_digitax_categories():
    assert classify_vat("VATABLE") == STANDARD
    assert classify_vat("Standard") == STANDARD
    assert classify_vat("7.5%") == STANDARD
    assert classify_vat("Yes") == STANDARD
    assert classify_vat("Exempt") == EXEMPT
    assert classify_vat("Non-VATable") == EXEMPT   # not read as "vatable"
    assert classify_vat("0") == EXEMPT
    assert classify_vat("Zero Rated") == ZERO
    assert classify_vat("0%") == ZERO
    # Blank / unknown -> '' so the UI flags it for approval
    assert classify_vat("") == ""
    assert classify_vat("dunno") == ""


def test_assign_item_codes_continues_client_sequence():
    existing = [ItemEntry(name="A", item_code="ITM_014", tax_category=STANDARD)]
    assert assign_item_codes(existing, 3) == ["ITM_015", "ITM_016", "ITM_017"]
    assert assign_item_codes([], 2) == ["ITM_001", "ITM_002"]


def test_assign_item_codes_with_variant_suffix():
    # Fresh catalogue gets -N codes that won't clash with the originals.
    assert assign_item_codes([], 3, suffix="-N") == ["ITM_001-N", "ITM_002-N", "ITM_003-N"]
    # Base codes are plain -> suffix appended once.
    base = [ItemEntry(name="A", item_code="ITM_014", tax_category=STANDARD)]
    assert assign_item_codes(base, 2, suffix="-N") == ["ITM_015-N", "ITM_016-N"]
    # Base already carries the marker -> continues it without doubling.
    varbase = [ItemEntry(name="A", item_code="ITM_014-N", tax_category=STANDARD)]
    assert assign_item_codes(varbase, 1, suffix="-N") == ["ITM_015-N"]
