import json

import pytest

pytest.importorskip("streamlit")

from ui.auth import ALL_CLIENTS, _configured_users, _parse_record


def test_admin_record_sees_all_clients():
    u = _parse_record("x", {"password": "secret", "role": "admin", "clients": ["geeta"]})
    assert u.is_admin
    assert u.clients == ALL_CLIENTS  # admins always see everything


def test_rep_record_scoped_to_clients():
    u = _parse_record("r", {"password": "ab", "role": "rep", "clients": ["geeta", "goldcoin"]})
    assert not u.is_admin
    assert u.clients == ["geeta", "goldcoin"]


def test_viewer_role_with_single_client_key():
    # "viewer" (any non-admin role) + singular "client" -> scoped list.
    u = _parse_record("geeta", {"password": "geeta2026", "role": "viewer", "client": "geeta"})
    assert not u.is_admin
    assert u.clients == ["geeta"]


def test_plaintext_and_hash_passwords_both_match():
    import hashlib
    plain = _parse_record("p", {"password": "geeta2026", "role": "viewer", "client": "geeta"})
    assert plain.matches("geeta2026") is True
    assert plain.matches("wrong") is False

    h = hashlib.sha256("topsecret".encode()).hexdigest()
    hashed = _parse_record("h", {"password": h, "role": "admin"})
    assert hashed.matches("topsecret") is True
    assert hashed.matches("nope") is False


def test_bare_string_is_backward_compatible_admin():
    u = _parse_record("legacy", "rabbi-change-me")
    assert u.is_admin
    assert u.clients == ALL_CLIENTS
    assert u.matches("rabbi-change-me")


def test_configured_users_parses_env(monkeypatch):
    monkeypatch.setenv("RABBI_USERS", json.dumps({
        "a": {"password": "x", "role": "admin"},
        "b": {"password": "y", "role": "rep", "clients": ["geeta"]},
    }))
    users, using_default = _configured_users()
    assert using_default is False
    assert users["a"].is_admin
    assert users["b"].clients == ["geeta"]
