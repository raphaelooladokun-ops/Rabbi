import json

import pytest

pytest.importorskip("streamlit")

from ui.auth import ALL_CLIENTS, _configured_users, _parse_record


def test_admin_record_sees_all_clients():
    u = _parse_record("x", {"password": "AB", "role": "admin", "clients": ["geeta"]})
    assert u.is_admin
    assert u.clients == ALL_CLIENTS  # admins always see everything
    assert u.password_hash == "ab"  # normalised lowercase


def test_rep_record_scoped_to_clients():
    u = _parse_record("r", {"password": "ab", "role": "rep", "clients": ["geeta", "goldcoin"]})
    assert not u.is_admin
    assert u.clients == ["geeta", "goldcoin"]


def test_rep_single_client_string_becomes_list():
    u = _parse_record("r", {"password": "ab", "role": "rep", "clients": "friendship"})
    assert u.clients == ["friendship"]


def test_bare_hash_is_backward_compatible_admin():
    u = _parse_record("legacy", "DEADBEEF")
    assert u.is_admin
    assert u.clients == ALL_CLIENTS
    assert u.password_hash == "deadbeef"


def test_configured_users_parses_env(monkeypatch):
    monkeypatch.setenv("RABBI_USERS", json.dumps({
        "a": {"password": "x", "role": "admin"},
        "b": {"password": "y", "role": "rep", "clients": ["geeta"]},
    }))
    users, using_default = _configured_users()
    assert using_default is False
    assert users["a"].is_admin
    assert users["b"].clients == ["geeta"]
