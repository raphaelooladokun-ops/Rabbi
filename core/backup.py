"""Full export / restore of all master data as a single JSON backup.

Works against either store backend (file or database) through the common
interface, so the operator always has a portable safety copy — independent of
where the data currently lives. Restore is tolerant of older/newer records
(unknown keys are ignored, missing ones use defaults).
"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .masters import ClientConfig, ItemEntry, PartyEntry

BACKUP_VERSION = 1


def _make(cls, data: dict):
    valid = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def export_all(store) -> dict[str, Any]:
    """Serialise clients, tax rates, and every client's items & parties."""
    clients = store.list_clients()
    return {
        "version": BACKUP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tax_rates": {k: str(v) for k, v in store.tax_rates().items()},
        "clients": [asdict(c) for c in clients],
        "items": {c.id: [asdict(e) for e in store.list_items(c.id)] for c in clients},
        "parties": {c.id: [asdict(e) for e in store.list_parties(c.id)] for c in clients},
    }


def export_json(store) -> str:
    return json.dumps(export_all(store), indent=2, ensure_ascii=False)


def import_all(store, data: dict[str, Any]) -> dict[str, int]:
    """Restore a backup into the store. Returns a count summary.

    Existing clients/tax-rates are overwritten; each client's items and parties
    are replaced with the backup's set.
    """
    summary = {"clients": 0, "items": 0, "parties": 0}

    rates = data.get("tax_rates") or {}
    if rates:
        store.save_tax_rates({k: Decimal(str(v)) for k, v in rates.items()})

    for c in data.get("clients", []):
        store.save_client(_make(ClientConfig, c))
        summary["clients"] += 1

    for client_id, items in (data.get("items") or {}).items():
        entries = [_make(ItemEntry, e) for e in items]
        store.seed_items(client_id, entries)
        summary["items"] += len(entries)

    for client_id, parties in (data.get("parties") or {}).items():
        entries = [_make(PartyEntry, p) for p in parties]
        store.seed_parties(client_id, entries)
        summary["parties"] += len(entries)

    return summary
