"""Reader interface and shared spreadsheet helpers.

A reader's only job is layout: turn one client's raw file into a flat list of
:class:`~core.models.LineRow` (one per item line), with the source-derived
fields populated. It performs no master lookups and makes no tax-filing
decisions — that is the engine's job. Anything a reader genuinely cannot
parse is attached to the row as a flag, never guessed.
"""
from __future__ import annotations

import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import pandas as pd

from ..models import LineRow

# A source can be a path or in-memory bytes (Streamlit upload).
Source = Union[str, bytes, io.BytesIO]


@dataclass
class ReadResult:
    """What a reader returns: the line rows plus any file-level problems."""

    rows: list[LineRow]
    file_errors: list[str] = field(default_factory=list)


def load_grid(
    source: Source,
    *,
    sheet_name: Union[str, int, None] = 0,
    engine: Optional[str] = None,
) -> pd.DataFrame:
    """Load a worksheet as a raw, header-less grid of cells.

    We read with ``header=None`` because these exports carry a multi-row
    company/title block above the real table; the reader locates the true
    header itself rather than trusting pandas to guess.
    """
    buf: Any = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    return pd.read_excel(
        buf, sheet_name=sheet_name, header=None, dtype=object, engine=engine
    )


def cell(grid: pd.DataFrame, row: int, col: Optional[int]) -> Any:
    """Safe single-cell access; returns ``None`` for out-of-range / NaN."""
    if col is None or row < 0 or row >= len(grid) or col < 0 or col >= grid.shape[1]:
        return None
    value = grid.iat[row, col]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


class Reader(ABC):
    """Base class for all per-client readers."""

    #: Default worksheet name/index; subclasses override.
    default_sheet: Union[str, int] = 0
    #: pandas engine hint; e.g. "xlrd" for legacy .xls.
    engine: Optional[str] = None

    @abstractmethod
    def read(self, source: Source, *, sheet_name: Union[str, int, None] = None) -> ReadResult:
        """Parse ``source`` into a :class:`ReadResult`."""
        raise NotImplementedError
