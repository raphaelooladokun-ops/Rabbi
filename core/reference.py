"""Bundled Digitax/FIRS reference data (states, LGAs, invoice types, HSN and
service codes) loaded from ``data/reference/*.csv``.

These ship with the app and are version-controlled so lookups (e.g. an LGA
name -> NG-XX-XXX code, or validating an HSN code) match Digitax exactly. All
loaders are cached; matching is punctuation/case-insensitive.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", str(text).lower())).strip()


def _digits(text: str) -> str:
    return re.sub(r"\D", "", str(text))


def _read(name: str) -> list[dict]:
    path = REFERENCE_DIR / name
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# -- states -----------------------------------------------------------------
@lru_cache(maxsize=1)
def _states() -> dict[str, str]:
    """Normalised state name -> NG-XX code (plus common aliases)."""
    out: dict[str, str] = {}
    for r in _read("state_codes.csv"):
        out[_norm(r["name"])] = r["code"].strip()
    # Helpful aliases for the FCT.
    if "abuja federal capital territory" in out:
        fc = out["abuja federal capital territory"]
        out.setdefault("abuja", fc)
        out.setdefault("fct", fc)
        out.setdefault("federal capital territory", fc)
    return out


def state_code_from_text(text: str) -> str:
    """Find a state name in free address text and return its NG-XX code."""
    low = f" {_norm(text)} "
    for name in sorted(_states(), key=len, reverse=True):
        if f" {name} " in low:
            return _states()[name]
    return ""


@lru_cache(maxsize=1)
def states() -> list[tuple[str, str]]:
    """(display name, NG-XX code) for all states, sorted by name (for dropdowns)."""
    rows = [(r["name"].strip(), r["code"].strip()) for r in _read("state_codes.csv") if r.get("name")]
    return sorted(rows, key=lambda x: x[0])


def state_name(code: str) -> str:
    for name, c in states():
        if c == code:
            return name
    return ""


# -- LGAs -------------------------------------------------------------------
@dataclass(frozen=True)
class Lga:
    name: str
    lga_code: str  # NG-XX-XXX
    state_code: str  # NG-XX


@lru_cache(maxsize=1)
def _lgas() -> list[Lga]:
    return [
        Lga(r["name"].strip(), r["lga_code"].strip(), r["state_code"].strip())
        for r in _read("lga_codes.csv") if r.get("name")
    ]


def all_lgas() -> list[Lga]:
    """Every LGA (name, NG-XX-XXX code, NG-XX state code)."""
    return list(_lgas())


@lru_cache(maxsize=64)
def lgas_for_state(state_code: str) -> list[tuple[str, str]]:
    """(LGA name, NG-XX-XXX code) within a state, sorted by name (for dropdowns)."""
    rows = [(l.name, l.lga_code) for l in _lgas() if l.state_code == state_code]
    return sorted(rows, key=lambda x: x[0])


def lga_name(lga_code: str) -> str:
    for lga in _lgas():
        if lga.lga_code == lga_code:
            return lga.name
    return ""


def lga_from_text(text: str, state_code: str = "") -> tuple[str, str]:
    """Find an LGA name in address text -> (lga_code, state_code).

    Prefers an LGA in ``state_code`` when given; longest name wins to avoid a
    short name matching inside a longer one. Returns ('', '') if none found.
    """
    low = f" {_norm(text)} "
    best: tuple[str, str] | None = None
    best_len = 0
    for lga in _lgas():
        if state_code and lga.state_code != state_code:
            continue
        nm = _norm(lga.name)
        if nm and f" {nm} " in low and len(nm) > best_len:
            best, best_len = (lga.lga_code, lga.state_code), len(nm)
    return best if best else ("", "")


# -- invoice types ----------------------------------------------------------
@lru_cache(maxsize=1)
def invoice_types() -> dict[str, str]:
    """Digitax invoice type code -> label."""
    return {r["code"].strip(): r["value"].strip() for r in _read("invoice_types.csv")}


def invoice_type_label(code: str) -> str:
    return invoice_types().get(str(code).strip(), "")


def invoice_type_code_for(label: str) -> str:
    """Reverse lookup: Digitax label -> code (case-insensitive). '' if none."""
    target = _norm(label)
    for code, value in invoice_types().items():
        if _norm(value) == target:
            return code
    return ""


# -- HSN product codes ------------------------------------------------------
@lru_cache(maxsize=1)
def _hsn_digit_set() -> set[str]:
    return {_digits(r["hscode"]) for r in _read("hsn_codes.csv") if r.get("hscode")}


def is_valid_hsn(code: str) -> bool:
    """True if the code matches a Digitax HSN entry (ignoring dots/spacing)."""
    d = _digits(code)
    return bool(d) and d in _hsn_digit_set()


def normalize_hsn(code: str) -> str:
    """Coerce an HSN code to the Digitax `xxxx.xx` format (6 digits).

    Keeps only digits, pads short codes with trailing zeros and truncates long
    ones to the 6-digit HS subheading, then inserts the dot. Blank stays blank
    (we never invent a code). Examples:
        "9031.8"  -> "9031.80"     "392020" -> "3920.20"
        "0101.21" -> "0101.21"     "29096010" (10-digit) -> "2909.60"
    """
    d = _digits(code)
    if not d:
        return ""
    d = (d + "000000")[:6]
    return f"{d[:4]}.{d[4:6]}"


# -- service codes ----------------------------------------------------------
@lru_cache(maxsize=1)
def service_codes() -> dict[str, str]:
    return {r["code"].strip(): r["description"].strip() for r in _read("service_codes.csv")}


def is_valid_service_code(code: str) -> bool:
    return str(code).strip() in service_codes()
