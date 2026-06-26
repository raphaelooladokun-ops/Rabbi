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
)
from .engine import InvoiceSummary


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
    return {
        "trader_invoice_number": iv.trader_invoice_number or "",
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
