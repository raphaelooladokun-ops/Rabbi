"""Common internal data model shared by every reader and the engine.

A *reader* turns one client's raw file into a list of :class:`LineRow`
(one per item line). The :class:`engine` then resolves, validates and
reconciles those rows and emits Digitax output rows. Nothing downstream of a
reader knows which client the data came from.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    """How a flag affects the run.

    ERROR   -> the line/invoice cannot be exported until resolved.
    WARNING -> exported by default but surfaced for the operator to review.
    """

    ERROR = "error"
    WARNING = "warning"


class FlagCode(str, Enum):
    """The fixed set of exception reasons surfaced on the review screen."""

    CUSTOMER_NOT_FOUND = "customer_not_found"
    ITEM_NOT_FOUND = "item_not_found"
    BROKEN_SOURCE_VALUE = "broken_source_value"
    INVOICE_TOTAL_MISMATCH = "invoice_total_mismatch"
    INVOICE_NUMBER_OVERFLOW = "invoice_number_overflow"
    TIN_FORMAT = "tin_format"
    FIELD_LENGTH_OVERFLOW = "field_length_overflow"
    MISSING_TAX_RATE = "missing_tax_rate"


@dataclass
class Flag:
    """A single thing the operator may need to resolve."""

    code: FlagCode
    severity: Severity
    message: str
    field: Optional[str] = None
    # Free-form context the UI can use to offer a resolution (e.g. the
    # unknown item name so the operator can add it to the master).
    context: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


@dataclass
class LineRow:
    """One item line in the common internal table.

    Readers populate the source-derived fields; the engine fills the
    resolved fields (``item_code``, ``tax_rate``, ``party_tin`` …) and
    appends :class:`Flag` objects for anything it cannot safely resolve.
    """

    # --- Provenance -------------------------------------------------------
    source_row: int  # 1-based row number in the original sheet, for the operator
    sheet: str = ""

    # --- Invoice identity -------------------------------------------------
    invoice_number_raw: str = ""  # exactly as written in the source
    branch: Optional[str] = None  # e.g. Goldcoin KETU/SAGAMU; part of invoice key
    invoice_date: Optional[date] = None

    # --- Customer ---------------------------------------------------------
    customer_name: str = ""
    customer_tin: Optional[str] = None
    tin_from_file: bool = False  # True if the TIN came straight from the source
    # Raw TIN / address text seen near the customer in the source (e.g. the
    # Tally "VAT No." column). Used only to PROPOSE a new B2B party — never to
    # silently set the TIN on output.
    customer_tin_hint: str = ""
    customer_address: str = ""

    # --- Item -------------------------------------------------------------
    item_name: str = ""
    item_hsn: Optional[str] = None
    item_code: Optional[str] = None
    tax_category: Optional[str] = None

    # --- Money (Decimal; unit_price is VAT-EXCLUSIVE) ---------------------
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    line_value: Optional[Decimal] = None  # VAT-exclusive line subtotal
    tax_rate: Optional[Decimal] = None  # e.g. Decimal("0.075"); 0 for exempt

    # --- Invoice-level reconciliation -------------------------------------
    # The invoice's stated total as carried on the source, forward-filled
    # onto every line of the invoice, used to reconcile. Some files state a
    # VAT-inclusive grand total and others a VAT-exclusive (taxable) total;
    # the reader sets ``stated_total_includes_vat`` so the engine compares
    # like with like.
    invoice_stated_total: Optional[Decimal] = None
    stated_total_includes_vat: bool = False
    # Some files (e.g. Friendship) state the total per item line rather than
    # once per invoice; the engine then sums these to get the invoice total.
    stated_total_is_per_line: bool = False

    # --- Engine output ----------------------------------------------------
    trader_invoice_number: Optional[str] = None  # after the 30-char rule
    invoice_kind: Optional[str] = None  # "B2B" / "B2C"

    flags: list[Flag] = dc_field(default_factory=list)
    raw: dict[str, Any] = dc_field(default_factory=dict)

    # ---------------------------------------------------------------------
    def add_flag(
        self,
        code: FlagCode,
        severity: Severity,
        message: str,
        field_name: Optional[str] = None,
        **context: Any,
    ) -> None:
        self.flags.append(
            Flag(code=code, severity=severity, message=message, field=field_name, context=context)
        )

    @property
    def invoice_key(self) -> tuple[str, str]:
        """Identity used to group lines into one invoice.

        Branch is part of the key because some clients (Goldcoin) run separate
        invoice sequences per branch and numbers can repeat across them.
        """
        return (self.branch or "", self.invoice_number_raw)

    @property
    def has_errors(self) -> bool:
        return any(f.is_error for f in self.flags)

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)
