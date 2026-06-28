"""Reader B — Friendship Co ("SALES LEDGER" sheet, .xlsx).

Flat layout: already one row per item line. A multi-line invoice is the same
Invoice No repeated on consecutive rows (the engine groups them). TIN and VAT
rate are present in the file and used directly; the item is resolved by the
engine from description or HSN against the items master.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Callable, Optional, Union

from ..models import FlagCode, LineRow, Severity
from ..parsing import ParseError, clean_str, is_blank, looks_like_tin, parse_date, parse_decimal
from .base import Reader, ReadResult, Source, cell, load_grid

# Ordered, most-specific-first column matchers. Each header cell is assigned
# to the first internal name whose predicate matches and is not yet taken.
_COLUMN_RULES: list[tuple[str, Callable[[str], bool]]] = [
    ("total_invoice", lambda t: "total invoice" in t),
    ("vat_amount", lambda t: "vat" in t and ("amount" in t or "amt" in t)),
    ("unit_price_incl", lambda t: "unit price" in t),
    ("invoice_no", lambda t: "invoice no" in t or "invoice number" in t),
    ("base_p", lambda t: "base p" in t),
    ("hsn", lambda t: "hsn" in t),
    ("uom", lambda t: "uom" in t),
    ("customer", lambda t: "customer" in t),
    ("category", lambda t: t == "category"),
    ("item_desc", lambda t: "description" in t or "item desc" in t),
    ("qty", lambda t: "qty" in t or "quantity" in t),
    ("vat_rate", lambda t: t == "vat" or t == "vat rate" or t == "vat %"),
    ("line_total", lambda t: t == "line total" or t == "total"),
    ("tin", lambda t: "tin" in t),
    ("del_date", lambda t: "date" in t),
]

_REQUIRED = {"invoice_no", "customer", "item_desc"}


def _find_header(grid, scan_rows: int = 25) -> tuple[int, dict[str, int]]:
    best_row, best_map, best_score = -1, {}, 0
    for r in range(min(scan_rows, len(grid))):
        colmap: dict[str, int] = {}
        for c in range(grid.shape[1]):
            text = clean_str(cell(grid, r, c)).lower()
            if not text:
                continue
            for name, pred in _COLUMN_RULES:
                if name in colmap:
                    continue
                if pred(text):
                    colmap[name] = c
                    break
        score = len(colmap)
        if score > best_score and _REQUIRED.issubset(colmap):
            best_row, best_map, best_score = r, colmap, score
    if best_row < 0:
        raise ValueError(
            "Could not find the SALES LEDGER header "
            "(expected columns including Invoice No, Customer Name, Item Description)."
        )
    return best_row, best_map


class FriendshipReader(Reader):
    default_sheet = "SALES LEDGER"

    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        sheet = self.default_sheet if sheet_name is None else sheet_name
        try:
            grid = load_grid(source, sheet_name=sheet)
        except (ValueError, KeyError):
            # Sheet name not found — fall back to the first sheet.
            grid = load_grid(source, sheet_name=0)
            sheet = 0

        try:
            header_row, cols = _find_header(grid)
        except ValueError as exc:
            return ReadResult(rows=[], file_errors=[str(exc)])

        # The pre-VAT line-total column sits between Base P and VAT and often
        # has a blank header; map it positionally if it wasn't named.
        if "line_total" not in cols and "base_p" in cols:
            candidate = cols["base_p"] + 1
            if candidate < grid.shape[1] and candidate not in cols.values():
                cols["line_total"] = candidate

        rows: list[LineRow] = []
        for r in range(header_row + 1, len(grid)):
            invoice_no = clean_str(cell(grid, r, cols.get("invoice_no")))
            item_desc = clean_str(cell(grid, r, cols.get("item_desc")))
            # Skip blank/structural rows: no invoice number and no item.
            if not invoice_no and not item_desc:
                continue

            row = self._build_row(grid, r, cols, sheet)
            rows.append(row)

        return ReadResult(rows=rows)

    # ------------------------------------------------------------------
    def _build_row(self, grid, r, cols, sheet) -> LineRow:
        sheet_row = r + 1
        row = LineRow(
            source_row=sheet_row,
            sheet=str(sheet),
            invoice_number_raw=clean_str(cell(grid, r, cols.get("invoice_no"))),
            customer_name=clean_str(cell(grid, r, cols.get("customer"))),
            item_name=clean_str(cell(grid, r, cols.get("item_desc"))),
            item_hsn=clean_str(cell(grid, r, cols.get("hsn"))) or None,
        )

        try:
            row.invoice_date = parse_date(
                cell(grid, r, cols.get("del_date")), field="del. date"
            )
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.WARNING, str(exc), "invoice_date")

        # Quantity and VAT-EXCLUSIVE unit price (Base P) — output unit_price
        # must be pre-VAT, so we use Base P, never the VAT-inclusive Unit Price.
        try:
            row.quantity = parse_decimal(cell(grid, r, cols.get("qty")), field="quantity")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "quantity")
        try:
            row.unit_price = parse_decimal(cell(grid, r, cols.get("base_p")), field="base price")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "unit_price")

        # Pre-VAT line value used in reconciliation = quantity x Base P.
        if row.unit_price is not None and row.quantity is not None:
            row.line_value = row.unit_price * row.quantity

        # VAT rate straight from the file's VAT column (0.075 / blank=0). We do
        # NOT derive Friendship's tax from the items master.
        row.tax_rate = self._parse_rate(grid, r, cols, row)

        # The ledger TIN is kept only as a HINT to pre-fill a new-party
        # proposal. The output TIN comes from the parties master, so an unknown
        # customer is treated as B2C until added there (with this TIN). Junk
        # like "NOT APPLICABLE" is ignored (must look like a real TIN).
        tin = clean_str(cell(grid, r, cols.get("tin")))
        if looks_like_tin(tin):
            row.customer_tin_hint = tin

        # Reconcile against the PRE-VAT invoice subtotal: the file's stated
        # pre-VAT line total (Base P x qty column), summed per invoice. VAT is
        # excluded from both sides, so no 7.5% drift.
        try:
            stated = parse_decimal(cell(grid, r, cols.get("line_total")), field="line total")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.WARNING, str(exc), "line_value")
            stated = None
        if stated is not None:
            row.invoice_stated_total = stated
            row.stated_total_includes_vat = False
            row.stated_total_is_per_line = True

        row.raw = {
            "category": clean_str(cell(grid, r, cols.get("category"))),
            "uom": clean_str(cell(grid, r, cols.get("uom"))),
            "unit_price_incl": cell(grid, r, cols.get("unit_price_incl")),
            "vat_amount": cell(grid, r, cols.get("vat_amount")),
        }
        return row

    @staticmethod
    def _parse_rate(grid, r, cols, row) -> Optional[Decimal]:
        raw = cell(grid, r, cols.get("vat_rate"))
        if is_blank(raw):
            return Decimal("0")  # blank VAT column => exempt line
        try:
            rate = parse_decimal(raw, field="vat rate")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.WARNING, str(exc), "tax_rate")
            return None
        if rate is None:
            return Decimal("0")
        # Accept either fraction (0.075) or percentage (7.5).
        return rate / Decimal("100") if rate > 1 else rate
