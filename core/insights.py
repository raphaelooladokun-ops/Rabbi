"""Admin-only customer insights derived from stored records.

This reads nothing new: it re-parses the *raw upload* files already kept in
the audit trail (``kind == "uploaded_raw"``) and rolls them up per customer,
so an admin can see which customers each client does the most business with.

It deliberately does NOT run the full engine (no master resolution, no tax
maths) — it only needs the customer name, the invoice identity and the pre-VAT
line value the reader already extracts, which keeps it fast even for the large
Goldcoin files. Client-facing workflow is untouched.

Only runs whose raw file was actually stored (records kept) are covered.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from .parsing import looks_like_tin
from .readers import get_reader

# Names that are inherently walk-in / cash, not an identifiable customer.
_CASH_NAMES = {"cash sales", "cash sale", "cash", "walk-in", "walk in customer"}


@dataclass
class CustomerStat:
    client_id: str
    customer_name: str
    kind: str  # "B2B" / "B2C" / "" (unknown)
    invoices: int
    total_ex_vat: Decimal
    first_date: Optional[date]
    last_date: Optional[date]

    @property
    def is_cash(self) -> bool:
        return self.customer_name.strip().lower() in _CASH_NAMES


def _run_uploads(store, client_id: Optional[str]):
    """Yield one ``uploaded_raw`` artifact per run (newest wins on duplicates)."""
    seen: set = set()
    for a in store.list_artifacts(client_id):
        if a.kind != "uploaded_raw" or a.run_id in seen:
            continue
        seen.add(a.run_id)
        yield a


def _kind_for(store, client_id: str, name: str) -> str:
    if name.strip().lower() in _CASH_NAMES:
        return "B2C"
    try:
        party = store.lookup_party(client_id, name)
    except Exception:  # noqa: BLE001
        return ""
    if party and party.status.upper() == "B2B" and looks_like_tin(party.tin):
        return "B2B"
    return "B2C" if party else ""


def records_signature(store, client_id: Optional[str] = None) -> tuple:
    """A cheap fingerprint of the stored uploads, so the UI can cache results
    and only recompute when the set of records actually changes."""
    return tuple(sorted(a.id for a in _run_uploads(store, client_id)))


def customer_stats(store, client_id: Optional[str] = None) -> list[CustomerStat]:
    """Roll up every stored raw upload into per-customer totals.

    Each real invoice is counted once even if it was uploaded more than once
    (the ``already_exists`` re-uploads are common here): invoices are keyed by
    (client, branch, number) and the newest upload wins, so re-submitting a
    period never inflates a customer's figures.
    """
    # invoice identity -> its captured customer / value / date, plus the run it
    # was captured from. list_artifacts is newest-first, so the first run to
    # touch an invoice is the most recent; older uploads of it are skipped.
    invoices: dict[tuple, dict] = {}
    for art in _run_uploads(store, client_id):
        try:
            _fname, content = store.read_artifact(art.id)
            client = store.get_client(art.client_id)
            if client is None:
                continue
            rows = get_reader(client.reader).read(content).rows
        except Exception:  # noqa: BLE001 — one unreadable record must not
            continue        # break the whole page; just skip it.

        for r in rows:
            name = (r.customer_name or "").strip()
            if not name:
                continue
            ikey = (art.client_id, r.branch or "", r.invoice_number_raw)
            inv = invoices.get(ikey)
            if inv is not None and inv["_run"] != art.run_id:
                continue  # already captured from a newer upload
            if inv is None:
                inv = {"client_id": art.client_id, "customer": name,
                       "value": Decimal("0"), "date": None, "_run": art.run_id}
                invoices[ikey] = inv
            if r.line_value is not None:
                inv["value"] += r.line_value
            if isinstance(r.invoice_date, date):
                inv["date"] = r.invoice_date

    agg: dict[tuple, dict] = {}
    for inv in invoices.values():
        cid, name = inv["client_id"], inv["customer"]
        key = (cid, name.lower())
        rec = agg.get(key)
        if rec is None:
            rec = {"client_id": cid, "name": name, "count": 0,
                   "total": Decimal("0"), "first": None, "last": None}
            agg[key] = rec
        rec["count"] += 1
        rec["total"] += inv["value"]
        d = inv["date"]
        if isinstance(d, date):
            if rec["first"] is None or d < rec["first"]:
                rec["first"] = d
            if rec["last"] is None or d > rec["last"]:
                rec["last"] = d

    out = [CustomerStat(
        client_id=rec["client_id"], customer_name=rec["name"],
        kind=_kind_for(store, rec["client_id"], rec["name"]),
        invoices=rec["count"], total_ex_vat=rec["total"],
        first_date=rec["first"], last_date=rec["last"],
    ) for rec in agg.values()]
    out.sort(key=lambda s: (s.invoices, s.total_ex_vat), reverse=True)
    return out
