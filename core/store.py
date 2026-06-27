"""Selects the master-data backend.

If a ``DATABASE_URL`` is configured (Streamlit secret or environment
variable) the database-backed store is used — required on hosts with an
ephemeral disk so masters persist. Otherwise the local JSON file store is
used, which needs no setup and is ideal on a laptop.
"""
from __future__ import annotations

import os
from typing import Union

from .masters import MasterStore


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    try:  # st.secrets raises if no secrets file exists
        import streamlit as st

        return str(st.secrets.get("DATABASE_URL", ""))  # type: ignore[attr-defined]
    except Exception:
        return ""


def get_master_store() -> Union[MasterStore, "object"]:
    url = _database_url()
    if url:
        from .masters_sql import SqlMasterStore

        return SqlMasterStore(url)
    return MasterStore()
