from __future__ import annotations

from src.auth import make_password_hash, verify_password


def test_password_hash_round_trip() -> None:
    encoded = make_password_hash("a-secure-test-password")
    assert verify_password("a-secure-test-password", encoded)
    assert not verify_password("wrong-password", encoded)

