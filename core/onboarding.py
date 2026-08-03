"""Onboarding helpers: turn a client's raw items list into a Digitax-ready
items master.

Pure, testable logic only (the Streamlit staging UI lives in ``app.py``):
detecting whether the raw list already states VAT/taxable status, mapping a
stated value to a Digitax tax-category code, and continuing the client's
``ITM_`` numbering. Nothing here guesses a tax status silently — an
unrecognised value returns "" so the UI can surface it for approval.
"""
from __future__ import annotations

import re

from .proposals import next_item_codes  # re-exported for the app / tests

# Digitax VAT-relevant tax categories we map onto.
STANDARD = "STANDARD_VAT"
ZERO = "ZERO_VAT"
EXEMPT = "EXEMPTED"

# Column names (normalised: lower-case, alphanumerics only) that indicate the
# raw list explicitly states VAT/taxable status. Most specific first; the broad
# "vat"/"tax" catch-alls are last.
VAT_COLUMN_ALIASES = (
    "vatstatus", "vatable", "vatablestatus", "taxable", "taxstatus", "vattype",
    "taxtype", "taxcategory", "taxcategorycode", "vatrate", "taxrate", "vat", "tax",
)


def _norm_col(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def find_vat_column(columns) -> str:
    """Return the original column name that states VAT status, or '' if none.

    ``columns`` is any iterable of the raw sheet's column headings.
    """
    norm = {_norm_col(c): c for c in columns}
    for alias in VAT_COLUMN_ALIASES:
        if alias in norm:
            return norm[alias]
    return ""


def classify_vat(value) -> str:
    """Map a stated VAT/taxable cell to a Digitax tax-category code.

    Returns STANDARD_VAT / ZERO_VAT / EXEMPTED, or '' when the value is blank or
    not understood (so the operator resolves it rather than us guessing).
    """
    s = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not s:
        return ""
    # Exempt / not-taxable wording — checked first so "non-vatable" isn't read
    # as "vatable".
    if any(k in s for k in ("exempt", "non-vat", "non vat", "nonvat", "no vat",
                            "not vat", "not applicable", "n/a", "nil", "none")):
        return EXEMPT
    if "zero" in s or s in ("0%", "0.0%"):
        return ZERO
    if any(k in s for k in ("standard", "vatable", "vat able", "taxable", "yes", "vat")):
        return STANDARD
    # A bare numeric rate: >0 is standard-rated; a stated 0 with no "zero"
    # wording is treated as exempt (the operator can flip it on approval).
    m = re.search(r"\d+(?:\.\d+)?", s)
    if m:
        return STANDARD if float(m.group()) > 0 else EXEMPT
    return ""


def assign_item_codes(existing_items, count: int) -> list[str]:
    """The next ``count`` ITM_ codes continuing the client's sequence."""
    return next_item_codes(existing_items, count)
