"""Static Digitax/NRS configuration and field constraints.

Values that are fixed by the Digitax upload format live here. Anything that
varies per client (item codes, TINs, tax category of a given item) lives in
the editable master data, not here.
"""
from __future__ import annotations

from decimal import Decimal

# Fixed Digitax constants.
INVOICE_TYPE_CODE = "388"  # commercial invoice
DOCUMENT_CURRENCY_CODE = "NGN"

# Default tax-category -> VAT rate map. NOT hardcoded at the line level: the
# rate is always looked up from the item's category here, and operators can
# extend this through the masters config. Reader B/C may instead carry an
# explicit per-line rate straight from the source file.
DEFAULT_TAX_RATES: dict[str, Decimal] = {
    "STANDARD_VAT": Decimal("0.075"),
    "EXEMPT": Decimal("0"),
    "ZERO_RATED": Decimal("0"),
}

# Exact Digitax CSV header, in order. "(optional)" annotations from the brief
# are dropped — these are the literal column names written to the file.
DIGITAX_COLUMNS: tuple[str, ...] = (
    "trader_invoice_number",
    "invoice_type_code",
    "invoice_date",
    "issue_date",
    "issue_time",
    "document_currency_code",
    "party_tin",
    "notes",
    "tax_point_date",
    "due_date",
    "accounting_cost",
    "payee_party_tin",
    "bill_party_tin",
    "ship_party_tin",
    "tax_representative_party_tin",
    "item_code",
    "quantity",
    "unit_price",
    "discount_rate",
    "fee_rate",
    "tax_rate",
    "callback_url",
    "payment_terms_note",
    "invoice_kind",
)

# Maximum character lengths Digitax accepts per field. Used by the engine to
# flag overflow the app cannot safely auto-fix. The 30-char invoice-number
# cap has its own dedicated trim rule (see engine.apply_invoice_number_rule).
FIELD_MAX_LENGTHS: dict[str, int] = {
    "trader_invoice_number": 30,
    "party_tin": 30,
    "item_code": 30,
    "notes": 255,
    "payment_terms_note": 255,
}

TRADER_INVOICE_NUMBER_MAX = FIELD_MAX_LENGTHS["trader_invoice_number"]

# Reconciliation tolerance: lines must sum to the stated invoice total within
# this absolute amount (covers cumulative rounding of per-line figures).
RECONCILE_TOLERANCE = Decimal("0.01")
