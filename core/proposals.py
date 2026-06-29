"""Create-missing-masters logic: turn unknown items/customers in a run into
reviewable *proposals* the operator approves before anything is written.

Nothing here auto-trusts a guess. Items are fuzzy-matched to the existing
master first (to avoid duplicate Digitax items); only genuinely-new items get
a drafted record and the next code in the client's sequence. New B2B parties
are only proposed when the real details (TIN + email + address) exist — TINs
and emails are never fabricated.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from typing import Iterable, Optional

from . import reference
from .masters import ItemEntry, PartyEntry

# Default similarity at/above which we treat an unknown item as the SAME as an
# existing one (spacing/punctuation/case/minor wording) rather than new.
DEFAULT_FUZZY_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for fuzzy compare."""
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", str(text).lower())).strip()


def _best_match(target: str, norm_items, sm: SequenceMatcher) -> tuple[Optional[ItemEntry], float]:
    """Closest item to a pre-normalised ``target`` over pre-normalised items.

    Uses difflib's cheap upper-bound gates (real_quick_ratio/quick_ratio) to
    skip the expensive ratio() for items that can't beat the current best —
    the same strategy as get_close_matches. Critical when the master is large
    (e.g. 8,000+ items), where the naive O(n) full-ratio scan takes seconds.
    """
    if not target:
        return None, 0.0
    sm.set_seq2(target)
    best: Optional[ItemEntry] = None
    best_score = 0.0
    for cand, entry in norm_items:
        if not cand:
            continue
        sm.set_seq1(cand)
        if sm.real_quick_ratio() <= best_score or sm.quick_ratio() <= best_score:
            continue
        score = sm.ratio()
        if score > best_score:
            best, best_score = entry, score
    return best, best_score


def _build_token_index(norm_items) -> dict[str, list]:
    """word -> [(norm_name, entry), ...] so we only compare items that share a
    word with the unknown (a real fuzzy match always shares at least one)."""
    idx: dict[str, list] = {}
    for cand, entry in norm_items:
        for tok in set(cand.split()):
            if len(tok) >= 2:
                idx.setdefault(tok, []).append((cand, entry))
    return idx


def _candidates(target: str, token_index: dict[str, list], cap: int = 1500) -> list:
    """Items sharing a word with ``target`` (rarest words first, capped)."""
    seen: set[int] = set()
    out: list = []
    tokens = sorted({t for t in target.split() if len(t) >= 2},
                    key=lambda t: len(token_index.get(t, ())))
    for tok in tokens:
        for cand, entry in token_index.get(tok, ()):
            if id(entry) not in seen:
                seen.add(id(entry))
                out.append((cand, entry))
        if len(out) >= cap:
            break
    return out


def fuzzy_best_item(name: str, items: list[ItemEntry]) -> tuple[Optional[ItemEntry], float]:
    """Return the closest existing item and its similarity score (0..1)."""
    norm_items = [(_norm(e.name), e) for e in items]
    target = _norm(name)
    cands = _candidates(target, _build_token_index(norm_items)) or norm_items
    return _best_match(target, cands, SequenceMatcher())


_CODE_RE = re.compile(r"^(.*?)(\d+)\s*$")


def _parse_code(code: str) -> Optional[tuple[str, int, int]]:
    """Split 'ITM_001' -> ('ITM_', 1, 3) = (prefix, number, zero-pad width)."""
    m = _CODE_RE.match(str(code).strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), len(m.group(2))


def next_item_codes(items: list[ItemEntry], count: int) -> list[str]:
    """Continue the client's existing code sequence, same prefix/width.

    Uses the dominant prefix among existing codes and the highest number in
    that prefix; pads to the widest existing number so the format matches.
    """
    parsed = [p for p in (_parse_code(e.item_code) for e in items if e.item_code) if p]
    if parsed:
        prefix = Counter(p[0] for p in parsed).most_common(1)[0][0]
        same = [p for p in parsed if p[0] == prefix]
        start = max(p[1] for p in same)
        width = max(p[2] for p in same)
    else:
        prefix, start, width = "ITM_", 0, 3
    return [f"{prefix}{str(start + i).zfill(width)}" for i in range(1, count + 1)]


def _most_common(values: Iterable[str]) -> str:
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else ""


@dataclass
class ItemProposal:
    name: str
    kind: str  # "possible_match" | "new"
    # possible_match:
    match_code: str = ""
    match_score: float = 0.0
    # new (drafted, all operator-editable before approval):
    item_code: str = ""
    item_category: str = ""
    hsn_code: str = ""
    description: str = ""
    tax_category_code: str = "STANDARD_VAT"
    is_service: bool = False


def propose_items(
    unknown_names: Iterable[str],
    items: list[ItemEntry],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    force_new: Iterable[str] = (),
) -> list[ItemProposal]:
    """Build one proposal per DISTINCT unknown item name.

    A close fuzzy match becomes a 'confirm mapping' proposal (no new code); the
    rest become 'new' proposals drafted from the master's existing patterns,
    each getting the next code in sequence (one code per physical item).
    """
    # Distinct, order-preserving.
    seen: set[str] = set()
    names: list[str] = []
    for n in unknown_names:
        k = _norm(n)
        if n and k not in seen:
            seen.add(k)
            names.append(n)

    default_category = _most_common(e.item_category for e in items)
    default_tax = _most_common(e.tax_category for e in items) or "STANDARD_VAT"
    forced = {_norm(n) for n in force_new}

    # Normalise the master once, build a word index, reuse one matcher — so
    # each unknown only compares against items sharing a word (fast at 8k+).
    norm_items = [(_norm(e.name), e) for e in items]
    token_index = _build_token_index(norm_items)
    sm = SequenceMatcher()

    proposals: list[ItemProposal] = []
    new_items: list[tuple[str, Optional[ItemEntry]]] = []
    for name in names:
        target = _norm(name)
        match, score = _best_match(target, _candidates(target, token_index), sm)
        if _norm(name) not in forced and match and score >= fuzzy_threshold:
            proposals.append(ItemProposal(
                name=name, kind="possible_match",
                match_code=match.item_code, match_score=round(score, 3),
                item_category=match.item_category, hsn_code=match.hsn_code,
                description=match.description, tax_category_code=match.tax_category,
                is_service=match.is_service,
            ))
        else:
            new_items.append((name, match))  # match = nearest item (below threshold)

    codes = next_item_codes(items, len(new_items))
    for (name, near), code in zip(new_items, codes):
        # Draft from the closest existing item so the HSN (in the master's
        # format), category and tax_category follow existing patterns. The
        # operator reviews/edits before approving.
        hsn = near.hsn_code if near else ""
        category = (near.item_category if near and near.item_category else default_category)
        tax = (near.tax_category if near and near.tax_category else default_tax)
        is_service = near.is_service if near else False
        proposals.append(ItemProposal(
            name=name, kind="new", item_code=code,
            item_category=category, hsn_code=hsn, description=name,
            tax_category_code=tax, is_service=is_service,
        ))
    return proposals


def item_entry_from_proposal(p: ItemProposal, matched: Optional[ItemEntry] = None) -> ItemEntry:
    """Materialise an approved proposal into a master ItemEntry.

    For a confirmed match we store the unknown spelling as an alias pointing at
    the existing code (copying the matched item's metadata).
    """
    if p.kind == "possible_match" and matched is not None:
        return ItemEntry(
            name=p.name, item_code=matched.item_code, hsn_code=matched.hsn_code,
            tax_category=matched.tax_category, item_category=matched.item_category,
            description=matched.description, is_service=matched.is_service,
        )
    return ItemEntry(
        name=p.name, item_code=p.item_code, hsn_code=p.hsn_code,
        tax_category=p.tax_category_code, item_category=p.item_category,
        description=p.description or p.name, is_service=p.is_service,
    )


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------
# Standard ISO 3166-2:NG state codes (safe to derive; the LGA third segment is
# Digitax-specific and is left for the operator to confirm, never invented).
NG_STATE_CODES: dict[str, str] = {
    "abia": "NG-AB", "adamawa": "NG-AD", "akwa ibom": "NG-AK", "anambra": "NG-AN",
    "bauchi": "NG-BA", "bayelsa": "NG-BY", "benue": "NG-BE", "borno": "NG-BO",
    "cross river": "NG-CR", "delta": "NG-DE", "ebonyi": "NG-EB", "edo": "NG-ED",
    "ekiti": "NG-EK", "enugu": "NG-EN", "gombe": "NG-GO", "imo": "NG-IM",
    "jigawa": "NG-JI", "kaduna": "NG-KD", "kano": "NG-KN", "katsina": "NG-KT",
    "kebbi": "NG-KE", "kogi": "NG-KO", "kwara": "NG-KW", "lagos": "NG-LA",
    "nasarawa": "NG-NA", "niger": "NG-NI", "ogun": "NG-OG", "ondo": "NG-ON",
    "osun": "NG-OS", "oyo": "NG-OY", "plateau": "NG-PL", "rivers": "NG-RI",
    "sokoto": "NG-SO", "taraba": "NG-TA", "yobe": "NG-YO", "zamfara": "NG-ZA",
    "abuja": "NG-FC", "fct": "NG-FC", "federal capital territory": "NG-FC",
}

# A Nigerian TIN: digits, optionally with a -NNNN branch suffix.
_TIN_RE = re.compile(r"\b(\d{8,15}-\d{4}|\d{8,15})\b")


def extract_tin(text: str) -> str:
    """Pull a TIN-looking token out of free address/VAT text; '' if none."""
    if not text:
        return ""
    m = _TIN_RE.search(str(text))
    return m.group(1) if m else ""


def derive_state_code(text: str) -> str:
    """Map a state name appearing in address text to its NG-XX code; '' if none.

    Uses the bundled Digitax state reference, falling back to a built-in map.
    """
    code = reference.state_code_from_text(text)
    if code:
        return code
    low = f" {_norm(text)} "
    for name in sorted(NG_STATE_CODES, key=len, reverse=True):
        if f" {name} " in low:
            return NG_STATE_CODES[name]
    return ""


def derive_lga(text: str, state_code: str = "") -> tuple[str, str]:
    """Find an LGA in address text -> (NG-XX-XXX code, NG-XX state). Best-effort."""
    return reference.lga_from_text(text, state_code)


@dataclass
class PartyProposal:
    name: str
    can_be_b2b: bool
    tin: str = ""
    email_address: str = ""
    street_name: str = ""
    city_name: str = ""
    postal_zone: str = ""
    country: str = "NGA"
    local_government: str = ""  # NG-XX-XXX — needs the LGA reference; left blank
    state: str = ""             # NG-XX — derived from the address when possible
    reason: str = ""            # why it stays B2C (when can_be_b2b is False)


def propose_party(
    name: str,
    *,
    tin_hint: str = "",
    email: str = "",
    address_text: str = "",
) -> PartyProposal:
    """Propose a new party from whatever real details are available.

    B2B requires TIN AND email AND address (none fabricated). Otherwise the
    customer stays B2C for this run, flagged as "could be B2B once details are
    obtained", and is not blocked.
    """
    tin = (tin_hint or extract_tin(address_text)).strip()
    email = (email or "").strip()
    address = (address_text or "").strip()
    state = derive_state_code(address)
    lga_code, lga_state = derive_lga(address, state)
    if lga_state and not state:
        state = lga_state  # back-fill the state from a matched LGA

    have_all = bool(tin and email and address)
    missing = [w for w, ok in (("TIN", tin), ("email", email), ("address", address)) if not ok]
    reason = "" if have_all else "could be B2B once details are obtained (missing: " + ", ".join(missing) + ")"
    return PartyProposal(
        name=name, can_be_b2b=have_all, tin=tin, email_address=email,
        street_name=address if have_all else "", city_name="", postal_zone="",
        country="NGA", local_government=lga_code, state=state, reason=reason,
    )


def party_entry_from_proposal(p: PartyProposal) -> PartyEntry:
    return PartyEntry(
        name=p.name, tin=p.tin, status="B2B" if p.can_be_b2b else "B2C",
        email_address=p.email_address, street_name=p.street_name, city_name=p.city_name,
        postal_zone=p.postal_zone, country=p.country or "NGA",
        local_government=p.local_government, state=p.state,
    )


# ---------------------------------------------------------------------------
def run_period(invoice_dates: Iterable[Optional[date]]) -> str:
    """A YYYYMM tag for output filenames, from the run's commonest month."""
    months = [d.strftime("%Y%m") for d in invoice_dates if isinstance(d, date)]
    if months:
        return Counter(months).most_common(1)[0][0]
    return date.today().strftime("%Y%m")
