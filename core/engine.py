"""Shared processing engine — runs after every reader.

Input: the common internal table (a list of :class:`LineRow`) plus the
client config and master store. Output: per-invoice summaries with resolved
codes/TINs, the applied invoice-number rule, reconciliation results, and
flags for everything that must go to the review screen. The engine never
silently guesses anything that affects a tax filing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .config import (
    FIELD_MAX_LENGTHS,
    RECONCILE_TOLERANCE,
    TRADER_INVOICE_NUMBER_MAX,
)
from .masters import ClientConfig, ItemEntry, MasterStore
from .models import FlagCode, Flag, LineRow, Severity

# Customer names that are inherently B2C — no party lookup, no flag.
_CASH_NAMES = {"cash sales", "cash sale", "cash", "walk-in", "walk in customer"}

# A TIN we consider well-formed: digits, then a 4-digit branch suffix.
_TIN_PATTERN = re.compile(r"^\d{8,15}-\d{4}$")
_TIN_NO_SUFFIX = re.compile(r"^\d{8,15}$")


@dataclass
class InvoiceSummary:
    """One grouped invoice and its resolution/validation outcome."""

    key: tuple[str, str]
    branch: Optional[str]
    invoice_number_raw: str
    invoice_date: object
    customer_name: str
    lines: list[LineRow] = field(default_factory=list)
    trader_invoice_number: Optional[str] = None
    invoice_kind: Optional[str] = None
    party_tin: Optional[str] = None
    ex_vat_total: Decimal = Decimal("0")
    gross_total: Decimal = Decimal("0")
    stated_total: Optional[Decimal] = None
    stated_includes_vat: bool = False
    flags: list[Flag] = field(default_factory=list)

    def add_flag(self, code, severity, message, field_name=None, **ctx) -> None:
        self.flags.append(Flag(code, severity, message, field_name, ctx))

    @property
    def all_flags(self) -> list[Flag]:
        out = list(self.flags)
        for ln in self.lines:
            out.extend(ln.flags)
        return out

    @property
    def has_errors(self) -> bool:
        return any(f.is_error for f in self.all_flags)

    @property
    def has_warnings(self) -> bool:
        return any(not f.is_error for f in self.all_flags)

    @property
    def ready(self) -> bool:
        """Exportable: a trader number is assigned and nothing blocks it."""
        return self.trader_invoice_number is not None and not self.has_errors


@dataclass
class ProcessResult:
    invoices: list[InvoiceSummary]

    @property
    def ready_invoices(self) -> list[InvoiceSummary]:
        return [iv for iv in self.invoices if iv.ready]

    @property
    def flagged_invoices(self) -> list[InvoiceSummary]:
        return [iv for iv in self.invoices if iv.has_errors or iv.has_warnings]

    @property
    def blocking_invoices(self) -> list[InvoiceSummary]:
        return [iv for iv in self.invoices if iv.has_errors]


# ---------------------------------------------------------------------------
def process(rows: list[LineRow], client: ClientConfig, store: MasterStore) -> ProcessResult:
    """Resolve, validate and reconcile reader output into invoice summaries."""
    for row in rows:
        _resolve_item(row, client, store)
        _resolve_party(row, client, store)

    invoices = _group_invoices(rows)
    _apply_invoice_number_rule(invoices)
    for iv in invoices:
        _reconcile(iv)
        _validate_field_lengths(iv)
    return ProcessResult(invoices=invoices)


# -- item resolution --------------------------------------------------------
def _resolve_item(row: LineRow, client: ClientConfig, store: MasterStore) -> None:
    entry = store.lookup_item(client.id, name=row.item_name, hsn=row.item_hsn or "")
    if entry is None:
        row.add_flag(
            FlagCode.ITEM_NOT_FOUND,
            Severity.ERROR,
            f"Item '{row.item_name}' is not in the items master; cannot assign an item_code.",
            "item_code",
            item_name=row.item_name,
            hsn=row.item_hsn or "",
        )
        return

    row.item_code = entry.item_code
    row.tax_category = entry.tax_category
    if not row.item_hsn:
        row.item_hsn = entry.hsn_code or None

    # Tax rate: if the reader carried an explicit per-line rate (Reader B),
    # keep it. Otherwise derive it from the item's tax category (Reader A/C).
    if row.tax_rate is None:
        rate = store.tax_rate_for(entry.tax_category)
        if rate is None:
            row.add_flag(
                FlagCode.MISSING_TAX_RATE,
                Severity.ERROR,
                f"No tax rate configured for category '{entry.tax_category}'.",
                "tax_rate",
                category=entry.tax_category,
            )
        else:
            row.tax_rate = rate


# -- party / TIN resolution -------------------------------------------------
def _resolve_party(row: LineRow, client: ClientConfig, store: MasterStore) -> None:
    name_norm = row.customer_name.strip().lower()

    # TIN already in the source file (Reader B): use it directly, validate.
    if row.tin_from_file and row.customer_tin:
        tin = _normalize_tin(row.customer_tin, client, row)
        row.customer_tin = tin
        row.invoice_kind = "B2B"
        return

    # Inherently-B2C names never need a party lookup.
    if name_norm in _CASH_NAMES:
        row.invoice_kind = "B2C"
        row.customer_tin = None
        return

    party = store.lookup_party(client.id, row.customer_name)
    if party and party.status.upper() == "B2B" and party.tin:
        row.customer_tin = _normalize_tin(party.tin, client, row)
        row.invoice_kind = "B2B"
        return
    if party:  # known, but B2C
        row.invoice_kind = "B2C"
        row.customer_tin = None
        return

    # Unknown customer. For B2B-expecting clients this is surfaced for review;
    # either way the safe fallback is B2C.
    if client.b2b_expected:
        row.add_flag(
            FlagCode.CUSTOMER_NOT_FOUND,
            Severity.WARNING,
            f"Customer '{row.customer_name}' is not in the parties master; treated as B2C.",
            "party_tin",
            customer_name=row.customer_name,
        )
    row.invoice_kind = "B2C"
    row.customer_tin = None


def _normalize_tin(tin: str, client: ClientConfig, row: LineRow) -> str:
    tin = tin.strip()
    if _TIN_PATTERN.match(tin):
        return tin
    # Apply an explicit, operator-confirmed suffix rule (e.g. append "-0001").
    rule = (client.tin_suffix_rule or "").strip()
    if rule and _TIN_NO_SUFFIX.match(tin):
        candidate = tin + (rule if rule.startswith("-") else "-" + rule)
        if _TIN_PATTERN.match(candidate):
            return candidate
    # Otherwise flag the format rather than guessing.
    row.add_flag(
        FlagCode.TIN_FORMAT,
        Severity.WARNING,
        f"TIN '{tin}' does not match the expected format and no safe normalisation rule applied.",
        "party_tin",
        tin=tin,
    )
    return tin


# -- grouping ---------------------------------------------------------------
def _group_invoices(rows: list[LineRow]) -> list[InvoiceSummary]:
    by_key: dict[tuple[str, str], InvoiceSummary] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = row.invoice_key
        iv = by_key.get(key)
        if iv is None:
            iv = InvoiceSummary(
                key=key,
                branch=row.branch,
                invoice_number_raw=row.invoice_number_raw,
                invoice_date=row.invoice_date,
                customer_name=row.customer_name,
                stated_total=row.invoice_stated_total,
                stated_includes_vat=row.stated_total_includes_vat,
            )
            by_key[key] = iv
            order.append(key)
        iv.lines.append(row)
        # Invoice-level fields are taken from the first line; keep a stated
        # total if any line carries one.
        if iv.stated_total is None and row.invoice_stated_total is not None:
            iv.stated_total = row.invoice_stated_total
            iv.stated_includes_vat = row.stated_total_includes_vat
    invoices = [by_key[k] for k in order]
    # invoice_kind: B2B if any line resolved to B2B with a TIN.
    for iv in invoices:
        b2b = next((ln for ln in iv.lines if ln.invoice_kind == "B2B" and ln.customer_tin), None)
        if b2b:
            iv.invoice_kind = "B2B"
            iv.party_tin = b2b.customer_tin
        else:
            iv.invoice_kind = "B2C"
            iv.party_tin = None
        # Push the decided kind/tin back onto every line for the writer.
        for ln in iv.lines:
            ln.invoice_kind = iv.invoice_kind
            ln.customer_tin = iv.party_tin
    return invoices


# -- invoice-number 30-char rule -------------------------------------------
def _apply_invoice_number_rule(invoices: list[InvoiceSummary]) -> None:
    """Cap trader_invoice_number at 30 chars, trimming from the end once per
    invoice, but never in a way that collides with another invoice's number.

    A silent merge would drop an invoice from the filing, so an unsafe trim
    is sent to review instead of being applied.
    """
    short = {iv.invoice_number_raw for iv in invoices if len(iv.invoice_number_raw) <= TRADER_INVOICE_NUMBER_MAX}

    long_invoices = [iv for iv in invoices if len(iv.invoice_number_raw) > TRADER_INVOICE_NUMBER_MAX]
    candidates: dict[tuple[str, str], str] = {
        iv.key: iv.invoice_number_raw[:TRADER_INVOICE_NUMBER_MAX] for iv in long_invoices
    }
    cand_counts: dict[str, int] = {}
    for c in candidates.values():
        cand_counts[c] = cand_counts.get(c, 0) + 1

    for iv in invoices:
        raw = iv.invoice_number_raw
        if len(raw) <= TRADER_INVOICE_NUMBER_MAX:
            iv.trader_invoice_number = raw
            continue
        cand = candidates[iv.key]
        collides = cand in short or cand_counts.get(cand, 0) > 1
        if collides:
            iv.trader_invoice_number = None
            iv.add_flag(
                FlagCode.INVOICE_NUMBER_OVERFLOW,
                Severity.ERROR,
                f"Invoice number '{raw}' exceeds {TRADER_INVOICE_NUMBER_MAX} chars and a safe "
                f"trim ('{cand}') would collide with another invoice; resolve manually.",
                "trader_invoice_number",
                raw=raw,
                attempted=cand,
            )
        else:
            iv.trader_invoice_number = cand
            short.add(cand)  # reserve it so later invoices don't reuse it


# -- reconciliation ---------------------------------------------------------
def _reconcile(iv: InvoiceSummary) -> None:
    ex = Decimal("0")
    gross = Decimal("0")
    for ln in iv.lines:
        value = ln.line_value if ln.line_value is not None else Decimal("0")
        rate = ln.tax_rate if ln.tax_rate is not None else Decimal("0")
        ex += value
        gross += value * (Decimal("1") + rate)
    iv.ex_vat_total = ex
    iv.gross_total = gross

    if iv.stated_total is None:
        return  # nothing to reconcile against
    basis = gross if iv.stated_includes_vat else ex
    if abs(basis - iv.stated_total) > RECONCILE_TOLERANCE:
        kind = "VAT-inclusive" if iv.stated_includes_vat else "VAT-exclusive"
        iv.add_flag(
            FlagCode.INVOICE_TOTAL_MISMATCH,
            Severity.WARNING,
            f"Line {kind} total {basis} does not match the stated invoice total "
            f"{iv.stated_total} (difference {basis - iv.stated_total}).",
            "invoice_stated_total",
            computed=str(basis),
            stated=str(iv.stated_total),
        )


# -- generic field-length validation ---------------------------------------
def _validate_field_lengths(iv: InvoiceSummary) -> None:
    if iv.party_tin and len(iv.party_tin) > FIELD_MAX_LENGTHS["party_tin"]:
        iv.add_flag(
            FlagCode.FIELD_LENGTH_OVERFLOW,
            Severity.WARNING,
            f"party_tin '{iv.party_tin}' exceeds {FIELD_MAX_LENGTHS['party_tin']} characters.",
            "party_tin",
        )
    for ln in iv.lines:
        if ln.item_code and len(ln.item_code) > FIELD_MAX_LENGTHS["item_code"]:
            ln.add_flag(
                FlagCode.FIELD_LENGTH_OVERFLOW,
                Severity.ERROR,
                f"item_code '{ln.item_code}' exceeds {FIELD_MAX_LENGTHS['item_code']} characters.",
                "item_code",
            )
