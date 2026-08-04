from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.auth import make_password_hash  # noqa: E402


if __name__ == "__main__":
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match.")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters.")
    print(make_password_hash(password))

