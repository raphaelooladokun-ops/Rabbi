"""Reader C — Goldcoin (Tally "Sales Register" export).

Treated exactly like Geeta for VAT/totals: the same parent/child Tally
structure, every figure pre-VAT, tax rate from the items master, and
reconciliation of the pre-VAT line sum against the pre-VAT subtotal.

The one Goldcoin-specific detail is invoice identity: the voucher type encodes
the branch, e.g. "SALES INVOICE (KETU)" vs "(SAGAMU)". Branches run separate
invoice sequences, so the branch is part of the invoice key
(``use_branch=True``) and the Voucher No. (e.g. ``GC/26/K/INV/00001``) is used
exactly as written — never renumbered. No TINs in the file: treated as B2C
unless the customer is found as B2B in a parties master.
"""
from __future__ import annotations

from typing import Union

from .base import Reader, ReadResult, Source
from .tally import parse_tally


class GoldcoinReader(Reader):
    default_sheet = 0
    engine = None  # auto-detect .xls / .xlsx from the file contents

    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        sheet = self.default_sheet if sheet_name is None else sheet_name
        return parse_tally(source, sheet_name=sheet, engine=self.engine, use_branch=True)
