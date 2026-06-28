"""Persistent, operator-editable master data.

Each client owns two reference lists plus a small config record. Everything
is stored as plain JSON under ``data/clients/<client_id>/`` so it survives
between runs and can be inspected by hand. Resolving an unknown item/party
during review appends it here, so future runs recognise it automatically.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .config import DEFAULT_TAX_RATES
from .parsing import normalize_key

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "clients"


@dataclass
class ItemEntry:
    """Maps a client's item name/description to a Digitax item_code.

    Carries the full Digitax item-template metadata so new items can be both
    written to the upload template and remembered for future runs. Older
    stored records (name/item_code/hsn_code/tax_category only) load fine — the
    extra fields default.
    """

    name: str  # item_name
    item_code: str
    hsn_code: str = ""
    tax_category: str = "STANDARD_VAT"  # tax_category_code
    item_category: str = ""
    description: str = ""
    is_service: bool = False


@dataclass
class PartyEntry:
    """Maps a customer name to a TIN/B2B-B2C status plus Digitax party fields."""

    name: str
    tin: str = ""  # tax_identification_number
    status: str = "B2C"  # "B2B" / "B2C"
    email_address: str = ""
    phone_number: str = ""
    street_name: str = ""
    city_name: str = ""
    postal_zone: str = ""
    country: str = "NGA"
    local_government: str = ""  # NG-XX-XXX
    state: str = ""  # NG-XX


@dataclass
class ClientConfig:
    """Per-client settings that drive reader choice and engine behaviour."""

    id: str
    name: str
    reader: str  # "geeta" | "friendship" | "goldcoin"
    b2b_expected: bool = False
    # Optional, operator-confirmed TIN normalisation rule for Reader B, e.g.
    # appending a missing branch suffix. Empty = no auto-normalisation.
    tin_suffix_rule: str = ""
    notes: str = ""


class MasterStore:
    """File-backed store for clients, item masters, party masters, tax rates.

    A single instance is safe to share within one process; mutations are
    guarded by a lock and written through to disk immediately.
    """

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- paths -------------------------------------------------------------
    @property
    def _registry_path(self) -> Path:
        return self.data_dir / "registry.json"

    @property
    def _tax_rates_path(self) -> Path:
        return self.data_dir / "tax_rates.json"

    def _client_dir(self, client_id: str) -> Path:
        d = self.data_dir / client_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _items_path(self, client_id: str) -> Path:
        return self._client_dir(client_id) / "items.json"

    def _parties_path(self, client_id: str) -> Path:
        return self._client_dir(client_id) / "parties.json"

    # -- low-level json ----------------------------------------------------
    @staticmethod
    def _read_json(path: Path, default):
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)  # atomic on POSIX

    # -- clients -----------------------------------------------------------
    def list_clients(self) -> list[ClientConfig]:
        raw = self._read_json(self._registry_path, [])
        return [ClientConfig(**c) for c in raw]

    def get_client(self, client_id: str) -> Optional[ClientConfig]:
        for c in self.list_clients():
            if c.id == client_id:
                return c
        return None

    def save_client(self, client: ClientConfig) -> None:
        with self._lock:
            clients = {c.id: c for c in self.list_clients()}
            clients[client.id] = client
            self._write_json(
                self._registry_path, [asdict(clients[k]) for k in clients]
            )

    # -- tax rates ---------------------------------------------------------
    def tax_rates(self) -> dict[str, Decimal]:
        raw = self._read_json(self._tax_rates_path, None)
        if raw is None:
            return dict(DEFAULT_TAX_RATES)
        return {k: Decimal(str(v)) for k, v in raw.items()}

    def save_tax_rates(self, rates: dict[str, Decimal]) -> None:
        with self._lock:
            self._write_json(
                self._tax_rates_path, {k: str(v) for k, v in rates.items()}
            )

    def tax_rate_for(self, category: Optional[str]) -> Optional[Decimal]:
        if not category:
            return None
        return self.tax_rates().get(category.strip().upper())

    # -- items -------------------------------------------------------------
    def list_items(self, client_id: str) -> list[ItemEntry]:
        raw = self._read_json(self._items_path(client_id), [])
        return [ItemEntry(**e) for e in raw]

    def lookup_item(
        self, client_id: str, *, name: str = "", hsn: str = ""
    ) -> Optional[ItemEntry]:
        """Resolve an item by name first, then (for Reader B) by HSN code."""
        items = self.list_items(client_id)
        name_key = normalize_key(name)
        if name_key:
            for e in items:
                if normalize_key(e.name) == name_key:
                    return e
        hsn_key = normalize_key(hsn)
        if hsn_key:
            for e in items:
                if e.hsn_code and normalize_key(e.hsn_code) == hsn_key:
                    return e
        return None

    def upsert_item(self, client_id: str, entry: ItemEntry) -> None:
        with self._lock:
            items = self.list_items(client_id)
            key = normalize_key(entry.name)
            for i, e in enumerate(items):
                if normalize_key(e.name) == key:
                    items[i] = entry
                    break
            else:
                items.append(entry)
            self._write_json(
                self._items_path(client_id), [asdict(e) for e in items]
            )

    # -- parties -----------------------------------------------------------
    def list_parties(self, client_id: str) -> list[PartyEntry]:
        raw = self._read_json(self._parties_path(client_id), [])
        return [PartyEntry(**e) for e in raw]

    def lookup_party(self, client_id: str, name: str) -> Optional[PartyEntry]:
        key = normalize_key(name)
        if not key:
            return None
        for e in self.list_parties(client_id):
            if normalize_key(e.name) == key:
                return e
        return None

    def upsert_party(self, client_id: str, entry: PartyEntry) -> None:
        with self._lock:
            parties = self.list_parties(client_id)
            key = normalize_key(entry.name)
            for i, e in enumerate(parties):
                if normalize_key(e.name) == key:
                    parties[i] = entry
                    break
            else:
                parties.append(entry)
            self._write_json(
                self._parties_path(client_id), [asdict(e) for e in parties]
            )

    # -- bulk seed ---------------------------------------------------------
    def seed_items(self, client_id: str, entries: list[ItemEntry]) -> None:
        with self._lock:
            self._write_json(
                self._items_path(client_id), [asdict(e) for e in entries]
            )

    def seed_parties(self, client_id: str, entries: list[PartyEntry]) -> None:
        with self._lock:
            self._write_json(
                self._parties_path(client_id), [asdict(e) for e in entries]
            )
