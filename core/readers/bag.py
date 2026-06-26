"""Reader C — Bag client (Tally "Sales Register" export, .xls).

Same parent/child structure as Reader A, with two differences:

* The voucher type encodes the branch, e.g. "SALES INVOICE (KETU)" vs
  "(SAGAMU)". Branches run separate invoice sequences, so the branch is part
  of the invoice key (``use_branch=True``) and the Voucher No.
  (e.g. ``GC/26/K/INV/00001``) is used exactly as written — never renumbered.
* No TINs in the file: treated as B2C unless the customer is found as B2B in
  a parties master (engine handles that lookup). Single 7.5% VAT, applied via
  the items master tax category like every other client.
"""
from __future__ import annotations

from typing import Union

from .base import Reader, ReadResult, Source
from .tally import parse_tally


class BagReader(Reader):
    default_sheet = 0
    engine = "xlrd"  # legacy .xls

    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        sheet = self.default_sheet if sheet_name is None else sheet_name
        return parse_tally(source, sheet_name=sheet, engine=self.engine, use_branch=True)
