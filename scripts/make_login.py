"""Create the RABBI_USERS secret (with roles and per-client access).

Run:  python scripts/make_login.py
It asks for one or more accounts and prints the single line you paste into
Streamlit's Secrets box (or into .streamlit/secrets.toml).

Roles:
  admin  -> sees all clients + Master data + Clients & settings
  rep    -> sees only the client ids you list (Convert + How-to)
"""
from __future__ import annotations

import getpass
import hashlib
import json


def main() -> None:
    users: dict[str, dict] = {}
    print("Add accounts. Leave the username blank when you're done.\n")
    while True:
        username = input("Username: ").strip()
        if not username:
            break
        password = getpass.getpass("Password (hidden): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("  Passwords did not match — try that user again.\n")
            continue
        role = input("Role [admin/rep] (default admin): ").strip().lower() or "admin"
        record = {"password": hashlib.sha256(password.encode("utf-8")).hexdigest(), "role": role}
        if role == "rep":
            raw = input("Client ids this rep can access (comma-separated, e.g. geeta,goldcoin): ").strip()
            record["clients"] = [c.strip() for c in raw.split(",") if c.strip()]
        users[username] = record
        print(f"  Added {username} ({role}).\n")

    if not users:
        print("No users added.")
        return

    print("\nPaste this single line into your Secrets:\n")
    print(f"RABBI_USERS = '{json.dumps(users)}'")


if __name__ == "__main__":
    main()
