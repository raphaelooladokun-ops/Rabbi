"""Reader A — Geeta (Tally "Sales Register" export, .xlsx).

Parent/child Tally layout. The branch is not used (Geeta runs a single
invoice sequence). Tax rate is left unset here and resolved by the engine
from each item's tax category in the items master — never hardcoded.
Customer TIN is resolved by the engine via the parties master.
"""
from __future__ import annotations

from typing import Union

from .base import Reader, ReadResult, Source
from .tally import parse_tally


class GeetaReader(Reader):
    default_sheet = 0  # first sheet

    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        sheet = self.default_sheet if sheet_name is None else sheet_name
        return parse_tally(source, sheet_name=sheet, use_branch=False)
