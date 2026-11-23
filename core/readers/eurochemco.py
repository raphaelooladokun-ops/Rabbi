"""Reader D — Eurochemco (ECVL) Tally "Sales Register" export (.xlsx).

A three-level Tally layout:

    Date | Particulars | Qty | Rate | Value | … | Vch Type | Vch No. | Debit | …
    (parent)  customer                                 branch     number   total
    (group)   "Toluene Sales"            (subtotal)
    (item)    item name   qty   rate   value

The quantity/rate/value sit in the (unlabelled) columns right after
Particulars; ``Value`` and the parent ``Debit`` are **VAT-inclusive**. There is
no tax column, so each item line carries its gross value and the engine backs
out the VAT once the item's rate is known from the master (STANDARD → ÷1.075,
EXEMPTED/ZERO → unchanged), reconciling against the parent's stated gross.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal
from typing import Optional, Union

import pandas as pd

from ..models import FlagCode, LineRow, Severity
from ..parsing import ParseError, clean_str, is_blank, parse_date, parse_decimal
from .base import Reader, ReadResult, Source, cell


def _find_header(grid, scan_rows: int = 25) -> tuple[int, dict[str, int]]:
    """Locate the header row and map internal names -> column indices.

    Quantity/Rate/Value are the three (often unlabelled) columns immediately
    after Particulars, so they are taken positionally.
    """
    for r in range(min(scan_rows, len(grid))):
        labels = {clean_str(cell(grid, r, c)).lower(): c for c in range(grid.shape[1])}
        part = next((labels[k] for k in labels if "particular" in k), None)
        vno = next((labels[k] for k in labels if "vch no" in k or "voucher no" in k), None)
        if part is None or vno is None:
            continue
        cols = {
            "date": next((labels[k] for k in labels if "date" in k), 0),
            "particulars": part,
            "quantity": part + 1,
            "rate": part + 2,
            "value": part + 3,
            "vch_type": next((labels[k] for k in labels if "vch type" in k or "voucher type" in k), None),
            "vch_no": vno,
            "debit": next((labels[k] for k in labels if "debit" in k), None),
        }
        if cols["value"] < grid.shape[1]:
            return r, cols
    raise ValueError(
        "Could not find a Eurochemco sales-register header "
        "(expected a Tally register with 'Particulars' and 'Vch No.' columns)."
    )


def _branch(vch_type: str) -> Optional[str]:
    """'Sales - Kano' -> 'Kano'; part of the invoice key so numbers that repeat
    across locations stay distinct."""
    b = re.sub(r"^\s*sales\s*-\s*", "", vch_type, flags=re.I).strip()
    return b or (vch_type or None)


def parse_eurochemco(source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
    buf = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    try:
        xls = pd.ExcelFile(buf)
    except Exception as exc:  # noqa: BLE001
        return ReadResult(rows=[], file_errors=[f"Could not open the workbook: {exc}"])

    sheets = xls.sheet_names if sheet_name is None else [sheet_name]
    grid = None
    header_row = -1
    cols: dict[str, int] = {}
    for sname in sheets:
        g = pd.read_excel(xls, sheet_name=sname, header=None, dtype=object)
        try:
            header_row, cols = _find_header(g)
            grid, sheet_name = g, sname
            break
        except ValueError:
            continue
    if grid is None:
        return ReadResult(rows=[], file_errors=[
            "Could not find a Eurochemco sales-register header "
            "(expected a Tally register with 'Particulars' and 'Vch No.' columns)."])

    rows: list[LineRow] = []
    ctx_date = ctx_customer = ctx_vno = None
    ctx_branch: Optional[str] = None
    ctx_total: Optional[Decimal] = None

    for r in range(header_row + 1, len(grid)):
        sheet_row = r + 1  # 1-based for the operator
        particulars = clean_str(cell(grid, r, cols["particulars"]))
        if not particulars or particulars.lower() in {"grand total", "total"}:
            continue

        try:
            row_date = parse_date(cell(grid, r, cols["date"]), field="date")
        except ParseError:
            row_date = None
        vno = clean_str(cell(grid, r, cols["vch_no"])) if cols["vch_no"] is not None else ""

        # --- Parent / invoice-header row (has a date and a voucher number) ---
        if row_date is not None and vno:
            ctx_date = row_date
            ctx_customer = particulars
            ctx_vno = vno
            vtype = clean_str(cell(grid, r, cols["vch_type"])) if cols["vch_type"] is not None else ""
            ctx_branch = _branch(vtype)
            try:
                ctx_total = parse_decimal(cell(grid, r, cols["debit"]), field="total") \
                    if cols["debit"] is not None else None
            except ParseError:
                ctx_total = None
            continue

        # --- Item line (has quantity and value); group subtotal rows have neither ---
        qty_raw = cell(grid, r, cols["quantity"])
        val_raw = cell(grid, r, cols["value"])
        if is_blank(qty_raw) or is_blank(val_raw):
            continue  # group/subtotal/blank row
        if ctx_vno is None:
            continue  # item before any invoice header — skip defensively

        row = LineRow(
            source_row=sheet_row,
            sheet=str(sheet_name),
            invoice_number_raw=ctx_vno,
            branch=ctx_branch,
            invoice_date=ctx_date,
            customer_name=ctx_customer or "",
            item_name=particulars,
            invoice_stated_total=ctx_total,       # VAT-inclusive parent total
            stated_total_includes_vat=True,
            raw={"quantity": qty_raw, "value": val_raw},
        )
        try:
            row.quantity = parse_decimal(qty_raw, field="quantity")
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "quantity")
        try:
            row.gross_amount = parse_decimal(val_raw, field="value")  # VAT-inclusive line value
        except ParseError as exc:
            row.add_flag(FlagCode.BROKEN_SOURCE_VALUE, Severity.ERROR, str(exc), "unit_price")
        rows.append(row)

    return ReadResult(rows=rows, file_errors=[])


class EurochemcoReader(Reader):
    default_sheet = 0

    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        # sheet_name=None lets the reader scan every sheet for the register.
        return parse_eurochemco(source, sheet_name=sheet_name)
