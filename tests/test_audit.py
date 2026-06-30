import pytest

from core.audit import new_run_id
from core.masters import MasterStore


def _exercise(store):
    run = new_run_id("geeta")
    a1 = store.save_artifact("geeta", run, "uploaded_raw", "sales.xls", b"RAWBYTES", "subomi")
    a2 = store.save_artifact("geeta", run, "invoices_csv", "geeta_invoices.csv", b"col1,col2\n1,2\n", "subomi")
    store.save_artifact("friendship", run, "invoices_csv", "f.csv", b"x", "rep")

    # list (newest first); filter by client
    geeta = store.list_artifacts("geeta")
    assert {a.kind for a in geeta} == {"uploaded_raw", "invoices_csv"}
    assert all(a.client_id == "geeta" for a in geeta)
    assert store.list_artifacts("friendship")[0].filename == "f.csv"
    assert len(store.list_artifacts()) == 3  # all clients

    # content round-trips
    fname, content = store.read_artifact(a1)
    assert fname == "sales.xls" and content == b"RAWBYTES"
    fname2, content2 = store.read_artifact(a2)
    assert content2 == b"col1,col2\n1,2\n"
    # metadata carries the user and a size
    rec = next(a for a in geeta if a.kind == "uploaded_raw")
    assert rec.username == "subomi" and rec.size == len(b"RAWBYTES")
    assert rec.label == "Uploaded sales file"


def test_audit_file_store(tmp_path):
    _exercise(MasterStore(tmp_path / "clients"))


def test_audit_sql_store(tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")  # noqa: F841
    from core.masters_sql import SqlMasterStore
    _exercise(SqlMasterStore(f"sqlite:///{tmp_path / 'a.db'}"))
