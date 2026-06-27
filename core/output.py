"""Digitax CSV writer — one row per item line.

Emits exactly the columns Digitax expects, in order (see
:data:`core.config.DIGITAX_COLUMNS`). Only invoices the operator has cleared
(``ready``) are written; the engine guarantees those have a trader number,
an item_code per line and a resolved invoice_kind.
"""
from __future__ import annotations

import csv
import io
from datetime import date, time
from decimal import Decimal
from typing import Iterable, Optional

from .config import (
    DIGITAX_COLUMNS,
    DOCUMENT_CURRENCY_CODE,
    INVOICE_TYPE_CODE,
    TRADER_INVOICE_NUMBER_MAX,
)
from .engine import InvoiceSummary, ProcessResult, dedupe_flags, flag_signature


def _fmt_date(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def _fmt_num(value: Optional[Decimal]) -> str:
    """Plain decimal string with no exponent or padding; blank for None."""
    if value is None:
        return ""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    d = d.normalize()
    # Avoid scientific notation for whole numbers like 1E+3.
    if d == d.to_integral_value():
        return str(d.quantize(Decimal("1")))
    return format(d, "f")


def _row_dict(iv: InvoiceSummary, line) -> dict[str, str]:
    party_tin = iv.party_tin if iv.invoice_kind == "B2B" else ""
    # Ready invoices always have a trader number. For the "all invoices"
    # export (which includes flagged ones) fall back to the truncated raw
    # number so the row is never blank; the operator fixes it in Excel.
    trader = iv.trader_invoice_number or iv.invoice_number_raw[:TRADER_INVOICE_NUMBER_MAX]
    return {
        "trader_invoice_number": trader,
        "invoice_type_code": INVOICE_TYPE_CODE,
        "invoice_date": _fmt_date(iv.invoice_date),
        "issue_date": _fmt_date(iv.invoice_date),
        "issue_time": "",
        "document_currency_code": DOCUMENT_CURRENCY_CODE,
        "party_tin": party_tin or "",
        "notes": "",
        "tax_point_date": "",
        "due_date": "",
        "accounting_cost": "",
        "payee_party_tin": "",
        "bill_party_tin": "",
        "ship_party_tin": "",
        "tax_representative_party_tin": "",
        "item_code": line.item_code or "",
        "quantity": _fmt_num(line.quantity),
        "unit_price": _fmt_num(line.unit_price),  # VAT-exclusive
        "discount_rate": "",
        "fee_rate": "",
        "tax_rate": _fmt_num(line.tax_rate),
        "callback_url": "",
        "payment_terms_note": "",
        "invoice_kind": iv.invoice_kind or "",
    }


def write_csv(invoices: Iterable[InvoiceSummary], *, only_ready: bool = True) -> str:
    """Render invoices to a Digitax CSV string."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(DIGITAX_COLUMNS), extrasaction="raise")
    writer.writeheader()
    for iv in invoices:
        if only_ready and not iv.ready:
            continue
        for line in iv.lines:
            writer.writerow(_row_dict(iv, line))
    return buf.getvalue()


def write_csv_bytes(invoices: Iterable[InvoiceSummary], *, only_ready: bool = True) -> bytes:
    return write_csv(invoices, only_ready=only_ready).encode("utf-8")


# --- Exceptions report -----------------------------------------------------
# Column order for the per-run list of things needing attention.
EXCEPTION_COLUMNS = (
    "severity", "issue", "invoice_number", "branch", "invoice_date",
    "customer", "item", "source_rows", "detail",
)


def build_exception_rows(result: ProcessResult) -> list[dict[str, str]]:
    """Flatten every flagged invoice into one report row per distinct issue.

    Each row points at the source line numbers in the operator's original
    file so they can jump straight to the cell to fix.
    """
    rows: list[dict[str, str]] = []
    for iv in result.invoices:
        if not iv.all_flags:
            continue
        # Map a flag's signature to the source rows of the lines it came from.
        src_by_sig: dict[tuple, list[int]] = {}
        for ln in iv.lines:
            for f in ln.flags:
                src_by_sig.setdefault(flag_signature(f), []).append(ln.source_row)

        for f in dedupe_flags(iv.all_flags):
            srcs = src_by_sig.get(flag_signature(f)) or [ln.source_row for ln in iv.lines]
            rows.append({
                "severity": "ERROR" if f.is_error else "warning",
                "issue": f.code.value,
                "invoice_number": iv.invoice_number_raw,
                "branch": iv.branch or "",
                "invoice_date": str(iv.invoice_date or ""),
                "customer": iv.customer_name,
                "item": f.context.get("item_name", ""),
                "source_rows": ", ".join(str(s) for s in sorted(set(srcs))),
                "detail": f.message,
            })
    # Errors first, then warnings; stable within each.
    rows.sort(key=lambda r: 0 if r["severity"] == "ERROR" else 1)
    return rows


def write_exceptions_csv(result: ProcessResult) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXCEPTION_COLUMNS))
    writer.writeheader()
    for row in build_exception_rows(result):
        writer.writerow(row)
    return buf.getvalue()
