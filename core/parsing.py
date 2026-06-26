"""Tolerant parsing of messy spreadsheet cell values.

Source files are human-maintained exports, so cells arrive as strings with
thousands separators, currency symbols, stray whitespace, Excel serial
dates, ``#N/A`` markers, etc. These helpers normalise them and raise
:class:`ParseError` on genuinely broken values so the engine can flag the
line rather than silently guessing.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# Markers that mean "no usable value" rather than a real datum.
_NA_MARKERS = {"", "#n/a", "n/a", "na", "nil", "none", "-", "--", "nan"}


class ParseError(ValueError):
    """Raised when a cell cannot be interpreted (vs. being legitimately blank)."""


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in _NA_MARKERS


def clean_str(value: Any) -> str:
    """Return a trimmed string, collapsing internal whitespace runs."""
    if is_blank(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_decimal(value: Any, *, field: str = "value") -> Optional[Decimal]:
    """Parse a money/quantity cell into Decimal.

    Returns ``None`` for a blank cell. Raises :class:`ParseError` for a
    value that is present but not numeric (e.g. a formula error string),
    because that is a *broken* source value the operator must look at.
    """
    if is_blank(value):
        return None
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return Decimal(str(value))

    text = str(value).strip()
    # Strip currency symbols/letters and thousands separators, keep digits,
    # sign, decimal point, and parentheses (accounting negatives).
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", "-", ".", "-."}:
        raise ParseError(f"{field}: cannot parse number from {value!r}")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ParseError(f"{field}: cannot parse number from {value!r}") from exc
    return -result if negative else result


# Excel/openpyxl already hands back datetime objects for date-typed cells,
# but .xls and some exports give serial numbers or strings.
_EXCEL_EPOCH = datetime(1899, 12, 30)  # accounts for the 1900 leap-year bug

_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d/%m/%y", "%d-%m-%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d-%B-%Y",
    "%m/%d/%Y",
)


def parse_date(value: Any, *, field: str = "date") -> Optional[date]:
    """Parse a date cell. Returns ``None`` if blank, raises if unparseable.

    Day-first formats are tried before US month-first, matching Nigerian /
    Tally / UK-style exports used by these clients.
    """
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial date.
        return (_EXCEL_EPOCH + timedelta(days=float(value))).date()

    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"{field}: cannot parse date from {value!r}")


def normalize_key(value: Any) -> str:
    """Canonical key for master lookups: case/space-insensitive."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()
