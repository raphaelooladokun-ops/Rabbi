"""Shared Tally "Sales Register" parsing (Reader A and Reader C).

Tally exports a multi-row title block, then a table whose rows alternate
between *parent* invoice rows (carry Date, customer in Particulars, Voucher
No. and invoice totals) and *child* item rows beneath them (blank Voucher
No., item name in Particulars, with Quantity/Rate/Value). This module finds
the header, walks the parent/child structure, and forward-fills the invoice
date / number / customer down onto each item line.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

from ..models import FlagCode, LineRow, Severity
from ..parsing import ParseError, clean_str, is_blank, parse_date, parse_decimal
from .base import ReadResult, Source, cell, load_grid

# Header label -> the internal column name we map it to. Matched as a
# case-insensitive substring against the header row's cells.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date",),
    "particulars": ("particulars",),
    "voucher_type": ("voucher type", "vch type"),
    "voucher_no": ("voucher no", "vch no"),
    "vat_no": ("vat no", "gstin", "tin"),
    "quantity": ("quantity", "qty"),
    "rate": ("rate",),
    "value": ("value",),
    "gross_total": ("gross total", "gross amount"),
}


def _find_header(grid, scan_rows: int = 40) -> tuple[int, dict[str, int]]:
    """Locate the table header row and map internal names -> column indices.

    The header is the row in the first ``scan_rows`` that matches the most
    known labels (it must include at least Particulars and Voucher No. to be
    a Tally sales-register header).
    """
    best_row, best_map, best_score = -1, {}, 0
    for r in range(min(scan_rows, len(grid))):
        colmap: dict[str, int] = {}
        for c in range(grid.shape[1]):
            text = clean_str(cell(grid, r, c)).lower()
            if not text:
                continue
            for name, aliases in _HEADER_ALIASES.items():
                if name in colmap:
                    continue
                if any(a in text for a in aliases):
                    colmap[name] = c
        score = len(colmap)
        if score > best_score and "particulars" in colmap and "voucher_no" in colmap:
            best_row, best_map, best_score = r, colmap, score
    if best_row < 0:
        raise ValueError(
            "Could not find a Tally sales-register header "
            "(expected columns including 'Particulars' and 'Voucher No.')."
        )
    return best_row, best_map


def _branch_from_voucher_type(voucher_type: str) -> Optional[str]:
    """Goldcoin encodes the branch in the voucher type, e.g.
    'SALES INVOICE (KETU)' -> 'KETU'. Geeta has none -> ``None``."""
    if "(" in voucher_type and ")" in voucher_type:
        inner = voucher_type[voucher_type.find("(") + 1 : voucher_type.rfind(")")]
        inner = inner.strip()
        if inner:
            return inner
    return None


def parse_tally(
    source: Source,
    *,
    sheet_name: Union[str, int, None] = 0,
    engine: Optional[str] = None,
    use_branch: bool = False,
) -> ReadResult:
    """Parse a Tally sales register into item-level :class:`LineRow` objects.

    ``use_branch`` controls whether the branch parsed from the voucher type
    becomes part of the invoice key (Reader C) or is ignored (Reader A).
    """
    grid = load_grid(source, sheet_name=sheet_name, engine=engine)
    file_errors: list[str] = []
    try:
        header_row, cols = _find_header(grid)
    except ValueError as exc:
        return ReadResult(rows=[], file_errors=[str(exc)])

    rows: list[LineRow] = []
    # Current invoice context, forward-filled onto item lines.
    ctx_date = ctx_customer = ctx_voucher = None
    ctx_branch: Optional[str] = None
    ctx_total: Optional[Decimal] = None
    ctx_tin_hint: str = ""

    for r in range(header_row + 1, len(grid)):
        sheet_row = r + 1  # 1-based for the operator
        voucher_no = clean_str(cell(grid, r, cols.get("voucher_no")))
        particulars = clean_str(cell(grid, r, cols.get("particulars")))

        # --- Parent / invoice-header row --------------------------------
        if voucher_no:
            try:
                ctx_date = parse_date(cell(grid, r, cols.get("date")), field="invoice date")
            except ParseError:
                ctx_date = None
            ctx_customer = particulars
            ctx_voucher = voucher_no
            ctx_tin_hint = clean_str(cell(grid, r, cols.get("vat_no")))
            voucher_type = clean_str(cell(grid, r, cols.get("voucher_type")))
            ctx_branch = _branch_from_voucher_type(voucher_type) if use_branch else None
            # Stated invoice total for reconciliation is the PRE-VAT subtotal
            # (the net/sales figure): every figure in these Tally exports is
            # pre-VAT, so we reconcile the pre-VAT line sum against it — never
            # against a VAT-inclusive gross (that caused the off-by-7.5%
            # mismatches). Prefer the assessable "Value" column on the parent;
            # fall back to whatever total column the export provides.
            try:
                parent_value = parse_decimal(cell(grid, r, cols.get("value")), field="value")
            except ParseError:
                parent_value = None
            try:
                parent_gross = parse_decimal(cell(grid, r, cols.get("gross_total")), field="total")
            except ParseError:
                parent_gross = None
            ctx_total = parent_value if parent_value is not None else parent_gross
            continue

        # --- Child item line (blank voucher no) -------------------------
        qty_raw = cell(grid, r, cols.get("quantity"))
        rate_raw = cell(grid, r, cols.get("rate"))
        value_raw = cell(grid, r, cols.get("value"))

        # Skip structural rows (blank lines, sub-totals with no item name).
        if not particulars or (is_blank(qty_raw) and is_blank(value_raw)):
            continue
        if ctx_voucher is None:
            # An item line before any invoice header — malformed file region.
            file_errors.append(
                f"Row {sheet_row}: item line '{particulars}' has no preceding invoice header."
            )
            continue

        row = LineRow(
            source_row=sheet_row,
            sheet=str(sheet_name),
            invoice_number_raw=ctx_voucher,
            branch=ctx_branch,
            invoice_date=ctx_date,
            customer_name=ctx_customer or "",
            customer_tin_hint=ctx_tin_hint,
            item_name=particulars,
            invoice_stated_total=ctx_total,
            stated_total_includes_vat=False,  # pre-VAT subtotal
            raw={"quantity": qty_raw, "rate": rate_raw, "value": value_raw},
        )

        try:
            row.quantity = parse_decimal(qty_raw, field="quantity")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "quantity")
        try:
            row.unit_price = parse_decimal(rate_raw, field="rate")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "unit_price")
        try:
            row.line_value = parse_decimal(value_raw, field="value")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "line_value")

        # Backfill a missing unit price or line value from the other two.
        if row.line_value is None and row.unit_price is not None and row.quantity:
            row.line_value = (row.unit_price * row.quantity)
        if row.unit_price is None and row.line_value is not None and row.quantity:
            row.unit_price = row.line_value / row.quantity

        rows.append(row)

    return ReadResult(rows=rows, file_errors=file_errors)
