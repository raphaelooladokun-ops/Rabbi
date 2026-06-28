"""Login + role/client scoping for the operator app.

``RABBI_USERS`` (Streamlit secret or env var) is a JSON map of username -> user
record::

    {
      "subomi":     {"password": "<sha256>", "role": "admin"},
      "geeta_rep":  {"password": "<sha256>", "role": "rep", "clients": ["geeta"]}
    }

* ``role`` is ``admin`` (all clients + Master data + Settings) or ``rep``
  (only their own client(s), Convert + How-to).
* ``clients`` lists the client ids a rep may see; admins see everything.

Backward compatible: a bare ``"username": "<sha256>"`` value is treated as an
admin with access to all clients. If nothing is configured, a default admin
login is used and a warning is shown.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

import streamlit as st

_DEFAULT_USER = "admin"
_DEFAULT_PASSWORD = "rabbi-change-me"
ALL_CLIENTS = "*"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class User:
    username: str
    password_hash: str
    role: str = "admin"  # "admin" | "rep"
    clients: object = ALL_CLIENTS  # ALL_CLIENTS or list[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _parse_record(username: str, value) -> User:
    # Bare hash string -> admin with all clients (back-compat).
    if isinstance(value, str):
        return User(username, value.lower(), "admin", ALL_CLIENTS)
    pw = str(value.get("password", "")).lower()
    role = str(value.get("role", "admin")).lower()
    clients = value.get("clients", ALL_CLIENTS)
    if role == "admin" or clients in (ALL_CLIENTS, None, "all"):
        clients = ALL_CLIENTS
    elif isinstance(clients, str):
        clients = [clients]
    return User(username, pw, role, clients)


def _configured_users() -> tuple[dict[str, User], bool]:
    raw = None
    try:  # st.secrets raises if no secrets file exists
        raw = st.secrets.get("RABBI_USERS")  # type: ignore[attr-defined]
    except Exception:
        raw = None
    if raw is None:
        raw = os.environ.get("RABBI_USERS")
    if raw:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return {u: _parse_record(u, v) for u, v in data.items()}, False
    return {_DEFAULT_USER: User(_DEFAULT_USER, _sha256(_DEFAULT_PASSWORD))}, True


def login_gate() -> bool:
    """Render the login form. Returns True once authenticated."""
    if st.session_state.get("authenticated"):
        return True

    users, using_default = _configured_users()
    st.title("Rabbi Consult — e-Invoicing Converter")
    st.subheader("Sign in")
    if using_default:
        st.warning(
            "No operator accounts configured. Using the default login "
            f"`{_DEFAULT_USER}` / `{_DEFAULT_PASSWORD}`. Set RABBI_USERS before going live "
            "(run `python scripts/make_login.py`)."
        )

    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        user = users.get(username)
        if user and user.password_hash == _sha256(password):
            st.session_state["authenticated"] = True
            st.session_state["username"] = username
            st.session_state["role"] = user.role
            st.session_state["clients"] = user.clients
            st.rerun()
        else:
            st.error("Invalid username or password.")
    return False


def current_role() -> str:
    return st.session_state.get("role", "admin")


def is_admin() -> bool:
    return current_role() == "admin"


def allowed_clients() -> object:
    """ALL_CLIENTS, or a list of client ids this user may access."""
    return st.session_state.get("clients", ALL_CLIENTS)


def logout_button() -> None:
    with st.sidebar:
        who = st.session_state.get("username", "")
        st.caption(f"Signed in as **{who}** ({current_role()})")
        if st.button("Sign out"):
            for k in ("authenticated", "username", "role", "clients"):
                st.session_state.pop(k, None)
            st.rerun()
