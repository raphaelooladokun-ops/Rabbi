"""Regression: an invoice that flags the same unknown customer/item on
several lines must collapse to one resolution form (no duplicate Streamlit
form keys)."""
import pytest

pytest.importorskip("streamlit")

from core.models import Flag, FlagCode, Severity


def test_dedupe_flags_collapses_repeats():
    from app import _dedupe_flags

    flags = [
        Flag(FlagCode.CUSTOMER_NOT_FOUND, Severity.WARNING, "x", "party_tin",
             {"customer_name": "Manichjane Plastic Enterprise Ltd."}),
        Flag(FlagCode.CUSTOMER_NOT_FOUND, Severity.WARNING, "x", "party_tin",
             {"customer_name": "Manichjane Plastic Enterprise Ltd."}),
        Flag(FlagCode.ITEM_NOT_FOUND, Severity.ERROR, "y", "item_code", {"item_name": "WIDGET"}),
        Flag(FlagCode.ITEM_NOT_FOUND, Severity.ERROR, "y", "item_code", {"item_name": "WIDGET"}),
        Flag(FlagCode.ITEM_NOT_FOUND, Severity.ERROR, "y", "item_code", {"item_name": "GADGET"}),
    ]
    out = _dedupe_flags(flags)
    assert len(out) == 3  # one customer, two distinct items
    names = {f.context.get("customer_name") or f.context.get("item_name") for f in out}
    assert names == {"Manichjane Plastic Enterprise Ltd.", "WIDGET", "GADGET"}
