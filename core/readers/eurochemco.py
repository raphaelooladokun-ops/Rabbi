"""Reader D — Eurochemco (ECVL) flat "Sales Register" export.

A one-line-per-sale register with a short title block then a table:

    DATE | PARTICULARS (customer) | PRODUCTS (item) | VCH NUMBER | AMOUNT | TIN

There is no quantity and no tax column, and ``AMOUNT`` is **VAT-inclusive**.
So each row is modelled as a single unit (quantity = 1) carrying the gross
amount; the engine backs out the VAT once the item's rate is known from the
master (STANDARD → ÷1.075, EXEMPTED/ZERO → unchanged). The customer TIN in the
file is only a hint — the output TIN always comes from the parties master.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

from ..models import FlagCode, LineRow, Severity
from ..parsing import ParseError, clean_str, is_blank, looks_like_tin, parse_date, parse_decimal
from .base import Reader, ReadResult, Source, cell, load_grid

# Header label -> internal name, matched as a case-insensitive substring.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date",),
    "customer": ("particulars", "customer"),
    "product": ("products", "product", "description"),
    "voucher": ("vch number", "vch no", "voucher", "invoice"),
    "amount": ("amount", "value", "total"),
    "tin": ("tin", "vat no"),
}


def _find_header(grid, scan_rows: int = 25) -> tuple[int, dict[str, int]]:
    """Locate the table header row and map internal names -> column indices.

    The header is the row (within the first ``scan_rows``) matching the most
    known labels; it must include the customer, product and amount columns.
    """
    best_row, best_map, best_score = -1, {}, 0
    for r in range(min(scan_rows, len(grid))):
        colmap: dict[str, int] = {}
        for c in range(grid.shape[1]):
            text = clean_str(cell(grid, r, c)).lower()
            if not text:
                continue
            for name, aliases in _HEADER_ALIASES.items():
                if name not in colmap and any(a in text for a in aliases):
                    colmap[name] = c
        score = len(colmap)
        if score > best_score and {"customer", "product", "amount"} <= set(colmap):
            best_row, best_map, best_score = r, colmap, score
    if best_row < 0:
        raise ValueError(
            "Could not find a Eurochemco sales-register header "
            "(expected columns including 'Particulars', 'Products' and 'Amount')."
        )
    return best_row, best_map


def parse_eurochemco(source: Source, *, sheet_name: Union[str, int, None] = 0) -> ReadResult:
    grid = load_grid(source, sheet_name=sheet_name)
    try:
        header_row, cols = _find_header(grid)
    except ValueError as exc:
        return ReadResult(rows=[], file_errors=[str(exc)])

    rows: list[LineRow] = []
    for r in range(header_row + 1, len(grid)):
        sheet_row = r + 1  # 1-based for the operator
        customer = clean_str(cell(grid, r, cols.get("customer")))
        product = clean_str(cell(grid, r, cols.get("product")))
        voucher = clean_str(cell(grid, r, cols.get("voucher")))
        amount_raw = cell(grid, r, cols.get("amount"))

        # Skip blank spacer rows and the trailing TOTAL row (which has an amount
        # but no product/voucher, and usually 'TOTAL' in the customer column).
        if customer.upper() == "TOTAL":
            continue
        if not product and not voucher:
            continue
        if not customer and not product:
            continue

        try:
            invoice_date = parse_date(cell(grid, r, cols.get("date")), field="date")
        except ParseError:
            invoice_date = None
        tin = clean_str(cell(grid, r, cols.get("tin")))

        row = LineRow(
            source_row=sheet_row,
            sheet=str(sheet_name),
            invoice_number_raw=voucher,
            invoice_date=invoice_date,
            customer_name=customer,
            customer_tin_hint=tin if looks_like_tin(tin) else "",
            item_name=product,
            quantity=Decimal("1"),  # register states no quantity
            stated_total_includes_vat=True,  # AMOUNT is VAT-inclusive
            stated_total_is_per_line=True,
            raw={"amount": amount_raw, "voucher": voucher},
        )
        try:
            gross = parse_decimal(amount_raw, field="amount")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "unit_price")
            gross = None
        if gross is not None:
            row.gross_amount = gross
            row.invoice_stated_total = gross  # reconcile against the stated gross

        rows.append(row)

    return ReadResult(rows=rows, file_errors=[])


class EurochemcoReader(Reader):
    default_sheet = 0

    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        sheet = self.default_sheet if sheet_name is None else sheet_name
        return parse_eurochemco(source, sheet_name=sheet)
