from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.repository import CsvRepository


class _Cursor:
    def __init__(self) -> None:
        self.last_sql = ""
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None) -> None:
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "RETURNING signature_id" in self.last_sql:
            return {
                "signature_id": "SIG-TEST",
                "username": "alice",
                "image_png": b"safe-png",
                "image_sha256": "digest",
                "created_at_utc": datetime.now(timezone.utc),
            }
        return None


def _repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CsvRepository, _Cursor]:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _Cursor()
    connection = nullcontext(type("Connection", (), {"cursor": lambda self: cursor})())
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    return repository, cursor


def test_signature_write_is_scoped_to_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, cursor = _repository(tmp_path, monkeypatch)

    saved = repository.save_user_signature(
        "alice",
        b"safe-png",
        actor_username="ALICE",
    )

    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert saved["username"] == "alice"
    assert "pg_advisory_xact_lock" in sql
    assert "INSERT INTO public.user_signatures" in sql
    assert "signature_saved" in sql


def test_another_user_cannot_change_or_remove_a_signature(tmp_path: Path) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )

    with pytest.raises(ValueError, match="own account"):
        repository.save_user_signature("alice", b"safe-png", actor_username="bob")
    with pytest.raises(ValueError, match="own account"):
        repository.remove_user_signature("alice", actor_username="bob")


def test_version_lookup_requires_both_signature_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, cursor = _repository(tmp_path, monkeypatch)

    repository.get_user_signature_version("SIG-ONE", expected_username="alice")

    sql, params = cursor.executed[-1]
    assert "signature_id = %s AND lower(username) = lower(%s)" in sql
    assert params == ("SIG-ONE", "alice")
