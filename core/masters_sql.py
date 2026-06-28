"""Database-backed master store (for hosted deployments).

Mirrors :class:`core.masters.MasterStore` exactly, but persists to any
SQLAlchemy-supported database instead of local JSON files. Use this on hosts
whose disk is ephemeral (e.g. Streamlit Community Cloud) by pointing
``DATABASE_URL`` at a free Postgres (Neon/Supabase). Locally, the file store
is fine and needs no setup.

The two stores are interchangeable: :func:`core.store.get_master_store`
selects one based on whether ``DATABASE_URL`` is configured.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    select,
)
from sqlalchemy.engine import Engine

from .config import DEFAULT_TAX_RATES
from .masters import ClientConfig, ItemEntry, PartyEntry
from .parsing import normalize_key

_metadata = MetaData()

clients_t = Table(
    "rabbi_clients", _metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(255)),
    Column("reader", String(64)),
    Column("b2b_expected", Boolean),
    Column("tin_suffix_rule", String(64)),
    Column("notes", String(2000)),
)

items_t = Table(
    "rabbi_items", _metadata,
    Column("client_id", String(64), primary_key=True),
    Column("name_key", String(512), primary_key=True),  # normalised lookup key
    Column("name", String(512)),
    Column("item_code", String(64)),
    Column("hsn_code", String(64)),
    Column("tax_category", String(64)),
    Column("item_category", String(255)),
    Column("description", String(1024)),
    Column("is_service", Boolean),
)

parties_t = Table(
    "rabbi_parties", _metadata,
    Column("client_id", String(64), primary_key=True),
    Column("name_key", String(512), primary_key=True),
    Column("name", String(512)),
    Column("tin", String(64)),
    Column("status", String(8)),
    Column("email_address", String(255)),
    Column("phone_number", String(64)),
    Column("street_name", String(512)),
    Column("city_name", String(255)),
    Column("postal_zone", String(32)),
    Column("country", String(8)),
    Column("local_government", String(32)),
    Column("state", String(32)),
)

tax_rates_t = Table(
    "rabbi_tax_rates", _metadata,
    Column("category", String(64), primary_key=True),
    Column("rate", String(32)),
)


class SqlMasterStore:
    """Drop-in, database-backed replacement for ``MasterStore``."""

    def __init__(self, database_url: str):
        # pool_pre_ping keeps connections healthy across a host's idle naps.
        self.engine: Engine = create_engine(database_url, pool_pre_ping=True, future=True)
        _metadata.create_all(self.engine)

    # -- clients -----------------------------------------------------------
    def list_clients(self) -> list[ClientConfig]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(clients_t).order_by(clients_t.c.id)).mappings().all()
        return [
            ClientConfig(
                id=r["id"], name=r["name"], reader=r["reader"],
                b2b_expected=bool(r["b2b_expected"]),
                tin_suffix_rule=r["tin_suffix_rule"] or "", notes=r["notes"] or "",
            )
            for r in rows
        ]

    def get_client(self, client_id: str) -> Optional[ClientConfig]:
        return next((c for c in self.list_clients() if c.id == client_id), None)

    def save_client(self, client: ClientConfig) -> None:
        values = dict(
            id=client.id, name=client.name, reader=client.reader,
            b2b_expected=client.b2b_expected, tin_suffix_rule=client.tin_suffix_rule,
            notes=client.notes,
        )
        with self.engine.begin() as conn:
            conn.execute(delete(clients_t).where(clients_t.c.id == client.id))
            conn.execute(clients_t.insert().values(**values))

    # -- tax rates ---------------------------------------------------------
    def tax_rates(self) -> dict[str, Decimal]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tax_rates_t)).mappings().all()
        if not rows:
            return dict(DEFAULT_TAX_RATES)
        return {r["category"]: Decimal(str(r["rate"])) for r in rows}

    def save_tax_rates(self, rates: dict[str, Decimal]) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(tax_rates_t))
            if rates:
                conn.execute(
                    tax_rates_t.insert(),
                    [{"category": k, "rate": str(v)} for k, v in rates.items()],
                )

    def tax_rate_for(self, category: Optional[str]) -> Optional[Decimal]:
        if not category:
            return None
        return self.tax_rates().get(category.strip().upper())

    # -- items -------------------------------------------------------------
    @staticmethod
    def _item_from_row(r) -> ItemEntry:
        return ItemEntry(
            name=r["name"], item_code=r["item_code"] or "", hsn_code=r["hsn_code"] or "",
            tax_category=r["tax_category"] or "STANDARD_VAT",
            item_category=r["item_category"] or "", description=r["description"] or "",
            is_service=bool(r["is_service"]),
        )

    @staticmethod
    def _item_values(client_id: str, e: ItemEntry) -> dict:
        d = asdict(e)
        d["client_id"] = client_id
        d["name_key"] = normalize_key(e.name)
        return d

    def list_items(self, client_id: str) -> list[ItemEntry]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(items_t).where(items_t.c.client_id == client_id).order_by(items_t.c.item_code)
            ).mappings().all()
        return [self._item_from_row(r) for r in rows]

    def lookup_item(self, client_id: str, *, name: str = "", hsn: str = "") -> Optional[ItemEntry]:
        name_key = normalize_key(name)
        with self.engine.connect() as conn:
            if name_key:
                r = conn.execute(
                    select(items_t).where(
                        items_t.c.client_id == client_id, items_t.c.name_key == name_key
                    )
                ).mappings().first()
                if r:
                    return self._item_from_row(r)
            hsn_key = normalize_key(hsn)
            if hsn_key:
                for r in conn.execute(
                    select(items_t).where(items_t.c.client_id == client_id)
                ).mappings():
                    if r["hsn_code"] and normalize_key(r["hsn_code"]) == hsn_key:
                        return self._item_from_row(r)
        return None

    def upsert_item(self, client_id: str, entry: ItemEntry) -> None:
        key = normalize_key(entry.name)
        with self.engine.begin() as conn:
            conn.execute(delete(items_t).where(
                items_t.c.client_id == client_id, items_t.c.name_key == key))
            conn.execute(items_t.insert().values(**self._item_values(client_id, entry)))

    def seed_items(self, client_id: str, entries: list[ItemEntry]) -> None:
        # De-duplicate by normalised name (last one wins), matching file store.
        unique: dict[str, ItemEntry] = {normalize_key(e.name): e for e in entries}
        with self.engine.begin() as conn:
            conn.execute(delete(items_t).where(items_t.c.client_id == client_id))
            if unique:
                conn.execute(items_t.insert(), [self._item_values(client_id, e) for e in unique.values()])

    # -- parties -----------------------------------------------------------
    @staticmethod
    def _party_from_row(r) -> PartyEntry:
        return PartyEntry(
            name=r["name"], tin=r["tin"] or "", status=r["status"] or "B2C",
            email_address=r["email_address"] or "", phone_number=r["phone_number"] or "",
            street_name=r["street_name"] or "", city_name=r["city_name"] or "",
            postal_zone=r["postal_zone"] or "", country=r["country"] or "NGA",
            local_government=r["local_government"] or "", state=r["state"] or "",
        )

    @staticmethod
    def _party_values(client_id: str, e: PartyEntry) -> dict:
        d = asdict(e)
        d["client_id"] = client_id
        d["name_key"] = normalize_key(e.name)
        return d

    def list_parties(self, client_id: str) -> list[PartyEntry]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(parties_t).where(parties_t.c.client_id == client_id).order_by(parties_t.c.name)
            ).mappings().all()
        return [self._party_from_row(r) for r in rows]

    def lookup_party(self, client_id: str, name: str) -> Optional[PartyEntry]:
        key = normalize_key(name)
        if not key:
            return None
        with self.engine.connect() as conn:
            r = conn.execute(
                select(parties_t).where(
                    parties_t.c.client_id == client_id, parties_t.c.name_key == key)
            ).mappings().first()
        return self._party_from_row(r) if r else None

    def upsert_party(self, client_id: str, entry: PartyEntry) -> None:
        key = normalize_key(entry.name)
        with self.engine.begin() as conn:
            conn.execute(delete(parties_t).where(
                parties_t.c.client_id == client_id, parties_t.c.name_key == key))
            conn.execute(parties_t.insert().values(**self._party_values(client_id, entry)))

    def seed_parties(self, client_id: str, entries: list[PartyEntry]) -> None:
        unique: dict[str, PartyEntry] = {normalize_key(e.name): e for e in entries}
        with self.engine.begin() as conn:
            conn.execute(delete(parties_t).where(parties_t.c.client_id == client_id))
            if unique:
                conn.execute(parties_t.insert(), [self._party_values(client_id, e) for e in unique.values()])
