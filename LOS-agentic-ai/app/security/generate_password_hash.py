"""
Run once to generate the value for DUMMY_PASSWORD_HASH in .env:

    python -m app.security.generate_password_hash

It will prompt for a password (hidden input) and print the hash string to
paste into .env. The plaintext password is never written anywhere.
"""

from __future__ import annotations

import getpass

from app.security.credential_store import hash_password


def main() -> None:
    password = getpass.getpass("Password to hash: ")
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Passwords did not match.")
        raise SystemExit(1)

    print("\nAdd this line to your .env:\n")
    print(f"DUMMY_PASSWORD_HASH={hash_password(password)}")


if __name__ == "__main__":
    main()