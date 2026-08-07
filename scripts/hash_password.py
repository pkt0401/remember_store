#!/usr/bin/env python3
"""Generate an APP_USERS_JSON-compatible PBKDF2 password hash."""

import getpass
import hashlib
import os


def main() -> None:
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not password:
        raise SystemExit("Password must not be empty.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    iterations = 600_000
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    print(f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}")


if __name__ == "__main__":
    main()
