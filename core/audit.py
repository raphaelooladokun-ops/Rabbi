"""Audit-trail artifacts: the raw files clients upload and the CSVs the app
generates, kept for the operator's records.

Stored through the same store backend (file or database), so on a hosted
deployment with DATABASE_URL they persist in the database and are downloadable
from the admin Records page.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

# Artifact kinds and their human labels.
KIND_LABELS = {
    "uploaded_raw": "Uploaded sales file",
    "invoices_csv": "Invoices CSV",
    "new_items_csv": "New items CSV",
    "new_parties_csv": "New parties CSV",
}


@dataclass
class Artifact:
    """Metadata for one stored file (content fetched separately on download)."""

    id: object
    run_id: str
    client_id: str
    kind: str
    filename: str
    username: str
    created_at: str
    size: int

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


def new_run_id(client_id: str) -> str:
    """A grouping id for all artifacts produced from one upload/run."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{client_id}-{stamp}-{uuid.uuid4().hex[:6]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
