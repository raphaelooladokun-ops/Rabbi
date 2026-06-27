"""Create the RABBI_USERS secret for operator logins.

Run:  python scripts/make_login.py
It asks for one or more username/password pairs and prints the single line you
paste into Streamlit's Secrets box (or into .streamlit/secrets.toml).
"""
from __future__ import annotations

import getpass
import hashlib
import json


def main() -> None:
    users: dict[str, str] = {}
    print("Add operator logins. Leave the username blank when you're done.\n")
    while True:
        username = input("Username: ").strip()
        if not username:
            break
        password = getpass.getpass("Password (typing is hidden): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("  Passwords did not match — try that user again.\n")
            continue
        users[username] = hashlib.sha256(password.encode("utf-8")).hexdigest()
        print(f"  Added {username}.\n")

    if not users:
        print("No users added.")
        return

    print("\nPaste this single line into your Secrets:\n")
    print(f"RABBI_USERS = '{json.dumps(users)}'")


if __name__ == "__main__":
    main()
