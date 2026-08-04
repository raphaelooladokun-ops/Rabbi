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
from decimal import Decimal, ROUND_HALF_UP
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


_CENT = Decimal("0.01")


def _round2(d: Decimal) -> Decimal:
    return d.quantize(_CENT, rounding=ROUND_HALF_UP)


def emit_quantity_price(line) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Return (quantity, unit_price) to write for a line.

    Digitax requires ``unit_price`` at no more than 2 decimal places. Most lines
    already satisfy that and pass through untouched. When a line carries a
    back-computed price with a long decimal tail (e.g. value / quantity), simply
    rounding the price would shift a large-quantity line's taxable total by a
    material amount. So for those lines we round the price to 2 dp *and* absorb
    the residual by re-deriving the quantity (also 2 dp) from the line's
    pre-VAT value, keeping ``unit_price × quantity`` equal to that value — the
    figure reconciliation is based on — to within a kobo.
    """
    q = line.quantity
    u = line.unit_price
    if u is None:
        return q, None
    u2 = _round2(u)
    if u2 == u or q is None:
        return q, u2  # already <=2dp (the common case) — nothing to absorb
    value = line.line_value if line.line_value is not None else (u * q)
    if u2 != 0 and value is not None:
        return _round2(value / u2), u2
    return q, u2


def _row_dict(
    iv: InvoiceSummary,
    line,
    invoice_type_code: str = INVOICE_TYPE_CODE,
    doc_date: Optional[date] = None,
) -> dict[str, str]:
    party_tin = iv.party_tin if iv.invoice_kind == "B2B" else ""
    # Ready invoices always have a trader number. For the "all invoices"
    # export (which includes flagged ones) fall back to the truncated raw
    # number so the row is never blank; the operator fixes it in Excel.
    trader = iv.trader_invoice_number or iv.invoice_number_raw[:TRADER_INVOICE_NUMBER_MAX]
    # Digitax does not allow backdating: invoice_date / issue_date carry the
    # processing date (today), formatted YYYY-MM-DD. The *actual* invoice date
    # from the sales register is preserved as the tax_point_date (blank if the
    # source had no parseable date).
    today = (doc_date or date.today()).isoformat()
    tax_point = _fmt_date(iv.invoice_date)
    out_qty, out_price = emit_quantity_price(line)
    return {
        "trader_invoice_number": trader,
        "invoice_type_code": invoice_type_code,
        "invoice_date": today,
        "issue_date": today,
        "issue_time(optional)": "",
        "document_currency_code": DOCUMENT_CURRENCY_CODE,
        "party_tin(optional)": party_tin or "",
        "notes(optional)": "",
        "tax_point_date(optional)": tax_point,
        "due_date(optional)": "",
        "accounting_cost(optional)": "",
        "payee_party_tin(optional)": "",
        "bill_party_tin(optional)": "",
        "ship_party_tin(optional)": "",
        "tax_representative_party_tin(optional)": "",
        "item_code": line.item_code or "",
        "quantity": _fmt_num(out_qty),
        "unit_price": _fmt_num(out_price),  # VAT-exclusive, <=2 dp
        "discount_rate(optional)": "",
        "fee_rate(optional)": "",
        "tax_rate": _fmt_num(line.tax_rate),
        "callback_url(optional)": "",
        "payment_terms_note(optional)": "",
        "invoice_kind": iv.invoice_kind or "",
    }


def write_csv(
    invoices: Iterable[InvoiceSummary],
    *,
    only_ready: bool = True,
    invoice_type_code: str = INVOICE_TYPE_CODE,
    doc_date: Optional[date] = None,
) -> str:
    """Render invoices to a Digitax CSV string.

    ``doc_date`` is the date stamped on every row (defaults to today, since
    Digitax does not allow backdating).
    """
    doc_date = doc_date or date.today()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(DIGITAX_COLUMNS), extrasaction="raise")
    writer.writeheader()
    for iv in invoices:
        if only_ready and not iv.ready:
            continue
        for line in iv.lines:
            writer.writerow(_row_dict(iv, line, invoice_type_code, doc_date))
    return buf.getvalue()


def write_csv_bytes(
    invoices: Iterable[InvoiceSummary],
    *,
    only_ready: bool = True,
    invoice_type_code: str = INVOICE_TYPE_CODE,
    doc_date: Optional[date] = None,
) -> bytes:
    return write_csv(
        invoices, only_ready=only_ready, invoice_type_code=invoice_type_code, doc_date=doc_date
    ).encode("utf-8")


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


# --- Digitax master upload templates (exact headers / column order) --------
ITEM_TEMPLATE_COLUMNS = (
    "item_name", "item_category", "hsn_code", "description",
    "tax_category_code", "item_code", "is_service",
)
PARTY_TEMPLATE_COLUMNS = (
    "tax_identification_number", "email_address", "name", "phone_number(optional)",
    "street_name", "city_name", "postal_zone", "country", "local_government", "state",
)


def _bool_str(value) -> str:
    return "TRUE" if value else "FALSE"


def write_items_template(entries: Iterable) -> str:
    """Write ItemEntry records in the exact Digitax item-template format."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(ITEM_TEMPLATE_COLUMNS))
    writer.writeheader()
    for e in entries:
        writer.writerow({
            "item_name": e.name,
            "item_category": e.item_category,
            "hsn_code": e.hsn_code,
            "description": e.description or e.name,
            "tax_category_code": e.tax_category,
            "item_code": e.item_code,
            "is_service": _bool_str(e.is_service),
        })
    return buf.getvalue()


def write_parties_template(entries: Iterable) -> str:
    """Write PartyEntry records in the exact Digitax party-template format."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(PARTY_TEMPLATE_COLUMNS))
    writer.writeheader()
    for e in entries:
        writer.writerow({
            "tax_identification_number": e.tin,
            "email_address": e.email_address,
            "name": e.name,
            "phone_number(optional)": e.phone_number,
            "street_name": e.street_name,
            "city_name": e.city_name,
            "postal_zone": e.postal_zone,
            "country": e.country or "NGA",
            "local_government": e.local_government,
            "state": e.state,
        })
    return buf.getvalue()
