"""Minimal login gate for the operator app.

Credentials come from Streamlit secrets or environment variables so no
password is ever committed. ``RABBI_USERS`` (secrets/env) is a mapping of
username -> sha256 password hash; if none is configured the app falls back to
a single ``admin`` account with a default password and shows a warning so the
operator changes it before going live.
"""
from __future__ import annotations

import hashlib
import json
import os

import streamlit as st

_DEFAULT_USER = "admin"
_DEFAULT_PASSWORD = "rabbi-change-me"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _configured_users() -> tuple[dict[str, str], bool]:
    """Return (username -> password-hash, using_default)."""
    raw = None
    try:  # st.secrets raises if no secrets file exists
        raw = st.secrets.get("RABBI_USERS")  # type: ignore[attr-defined]
    except Exception:
        raw = None
    if raw is None:
        raw = os.environ.get("RABBI_USERS")
    if raw:
        users = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return {u: h.lower() for u, h in users.items()}, False
    return {_DEFAULT_USER: _sha256(_DEFAULT_PASSWORD)}, True


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
            f"`{_DEFAULT_USER}` / `{_DEFAULT_PASSWORD}`. Set RABBI_USERS "
            "(a JSON map of username to sha256 password hash) before going live."
        )

    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if users.get(username) == _sha256(password):
            st.session_state["authenticated"] = True
            st.session_state["username"] = username
            st.rerun()
        else:
            st.error("Invalid username or password.")
    return False


def logout_button() -> None:
    with st.sidebar:
        st.caption(f"Signed in as **{st.session_state.get('username', '')}**")
        if st.button("Sign out"):
            for k in ("authenticated", "username"):
                st.session_state.pop(k, None)
            st.rerun()
