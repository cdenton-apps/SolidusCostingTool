from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.repository import CsvRepository


PROJECT_DATA = Path(__file__).resolve().parents[1] / "data"


class _FakeCursor:
    def __init__(self, *, history_records=None):
        self.history_records = history_records or []
        self.last_sql = ""
        self.executed = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "COALESCE(MAX(revision)" in self.last_sql:
            return {"revision": 1}
        if "RETURNING record" in self.last_sql:
            return {"record": {"costing_id": "C-DB-ONE", "esign_status": "sent"}}
        return None

    def fetchall(self):
        if "SELECT record FROM public.costing_revisions" in self.last_sql:
            return [{"record": record} for record in self.history_records]
        return []


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def test_saves_append_only_revisions(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    record = {
        "item_code": "ITEM-001",
        "description": "A test item",
        "customer_name": "Customer",
        "pricing_base_per_1000": 100,
        "spread_percent": 30,
        "selling_price_per_1000": 150,
    }

    first = repository.save_costing(
        record,
        user_username="one",
        user_email="one@example.com",
        user_name="User One",
    )
    second = repository.save_costing(
        {**record, "selling_price_per_1000": 160},
        user_username="two",
        user_email="two@example.com",
        user_name="User Two",
    )
    history = repository.load_history()

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert len(history) == 2
    assert list(history["created_by_username"]) == ["one", "two"]
    assert list(history["created_by_name"]) == ["User One", "User Two"]
    assert list(pd.to_numeric(history["selling_price_per_1000"])) == [150, 160]


def test_neon_save_uses_database_without_writing_history_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(
        repository, "_connect", lambda: _FakeConnection(cursor)
    )

    saved = repository.save_costing(
        {"item_code": "DB-001", "customer_name": "Database Customer"},
        user_username="alice",
        user_email="alice@example.com",
        user_name="Alice",
    )

    assert repository.uses_database
    assert saved["revision"] == 1
    assert any(
        "INSERT INTO public.costing_revisions" in sql
        for sql, _ in cursor.executed
    )
    assert not repository.history_path.exists()


def test_neon_history_reconstructs_saved_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {
        "costing_id": "C-DB-ONE",
        "item_code": "DB-001",
        "revision": 1,
        "created_by": "alice@example.com",
        "created_by_username": "alice",
        "created_by_name": "Alice",
        "selling_price_per_1000": 150,
    }
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor(history_records=[record])
    monkeypatch.setattr(
        repository, "_connect", lambda: _FakeConnection(cursor)
    )

    history = repository.load_history()

    assert list(history["costing_id"]) == ["C-DB-ONE"]
    assert list(history["created_by_username"]) == ["alice"]


def test_esign_status_is_attached_to_owned_neon_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    updated = repository.update_costing_esign(
        "C-DB-ONE",
        {"esign_status": "sent", "not_allowed": "discarded"},
        owner_email="alice@example.com",
    )

    sql, params = cursor.executed[-1]
    assert "UPDATE public.costing_revisions" in sql
    assert "lower(created_by_email)" in sql
    assert params[1:] == ("C-DB-ONE", "alice@example.com")
    assert updated["esign_status"] == "sent"


def test_neon_user_creation_writes_user_and_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    repository.save_app_user(
        username="newuser",
        email="newuser@example.com",
        name="New User",
        password_hash="pbkdf2_sha256$600000$salt$digest",
        role="creator",
        can_view_history=False,
        is_active=True,
        must_change_password=True,
        actor_username="admin",
    )

    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "INSERT INTO public.app_users" in sql
    assert "INSERT INTO public.app_audit_log" in sql


def test_simultaneous_users_receive_distinct_revisions(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)

    def save(index: int) -> dict:
        return repository.save_costing(
            {
                "item_code": "SHARED-001",
                "description": "Shared item",
                "customer_name": f"Customer {index}",
                "selling_price_per_1000": 150 + index,
            },
            user_username=f"user{index}",
            user_email=f"user{index}@example.com",
            user_name=f"User {index}",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        saved = list(pool.map(save, range(6)))

    history = repository.load_history()
    assert sorted(item["revision"] for item in saved) == [1, 2, 3, 4, 5, 6]
    assert len({item["costing_id"] for item in saved}) == 6
    assert len(history) == 6


def test_user_history_is_private_and_case_insensitive(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    repository.save_costing(
        {"item_code": "MINE-001", "customer_name": "My customer"},
        user_username="alice",
        user_email="Alice@Example.com",
        user_name="Alice",
    )
    repository.save_costing(
        {"item_code": "THEIRS-001", "customer_name": "Other customer"},
        user_username="other",
        user_email="other@example.com",
        user_name="Other User",
    )

    mine = repository.load_user_history("alice@example.com")
    assert list(mine["item_code"]) == ["MINE-001"]
    assert set(mine["created_by"]) == {"Alice@Example.com"}
    assert set(mine["created_by_username"]) == {"alice"}


def test_saved_item_appears_in_catalog(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    repository.save_costing(
        {
            "item_code": "NEW-001",
            "description": "New item",
            "materials_cost_per_1000": 125.0,
        },
        user_email="one@example.com",
        user_name="User One",
    )
    catalog = repository.load_catalog()
    assert catalog.iloc[0]["item_code"] == "NEW-001"
    assert catalog.iloc[0]["source_type"] == "Saved costing"


def test_catalog_hides_stock_items_without_a_costing_bom() -> None:
    repository = CsvRepository(PROJECT_DATA)
    catalog = repository.load_catalog()
    stock_items = catalog[catalog["source_type"] == "Stock list"]

    assert len(stock_items) == 581
    assert pd.to_numeric(stock_items["bom_available"]).gt(0).all()
    assert "BOX001/103/YPL/B0070/01/950G" not in set(stock_items["item_code"])


def test_machine_time_prefers_effective_quantity_per_run() -> None:
    bom = pd.DataFrame(
        [
            {
                "bom_code": "SPEED-TEST",
                "cost_type": "Machine",
                "is_informational_row": 0,
                "run_hours": 1.0,
                "system_quantity_per_run": 10.0,
                "effective_quantity_per_run": 5.0,
                "cost_rate": 100.0,
                "machine_bucket": "Die Cut",
                "cost_description": "Die cutting",
            }
        ]
    )

    result = CsvRepository._machine_time_from_frames("SPEED-TEST", bom)

    assert result["machine_hours_per_1000"] == pytest.approx(0.2)
    assert result["machine_time_source"] == "BOM operation speeds"

    details = CsvRepository._machine_time_details_from_frames("SPEED-TEST", bom)
    line = details["lines"].iloc[0]
    assert line["effective_quantity_per_run"] == pytest.approx(5)
    assert line["quantity_source"] == "Column Q — effective quantity"
    assert line["hours_per_1000"] == pytest.approx(0.2)


def test_supplied_item_and_bom_feeds_reconcile() -> None:
    repository = CsvRepository(PROJECT_DATA)
    items = repository.load_current_items()
    boards = repository.load_board_items()
    prices = repository.load_board_prices()
    item = items[items["item_code"] == "BOX001/101/LPB/1000G/1240P"].iloc[0]

    assert len(items) == 1305
    assert len(boards) == 986
    assert len(prices) == 1163
    assert pd.to_numeric(boards["price_per_tonne"], errors="coerce").notna().sum() == 579
    assert int(items["bom_available"].sum()) == 581
    assert "BOXT701/102/YPT/1000G/1800P" in set(items["item_code"])
    assert item["pallet_quantity"] == 1240
    assert item["materials_cost_per_1000"] == pytest.approx(488.2616)
    assert item["imported_machine_cost_per_1000"] == 66.92
    assert item["labour_cost_per_1000"] == 51.09
    assert item["imported_bom_total_per_1000"] == 606.27
    assert item["machine_hours_per_1000"] == pytest.approx(0.375958)


def test_board_code_lookup_returns_known_dimensions_and_price() -> None:
    repository = CsvRepository(PROJECT_DATA)

    board = repository.find_board_by_code("4-15614/", manufacturing_site="101")

    assert board is not None
    assert board["board_item_code"] == "BRD001/101/LPB/1000G/BW"
    assert board["board_code"] == "4-15614"
    assert board["board_width_mm"] == pytest.approx(1358)
    assert board["board_length_mm"] == pytest.approx(878)
    assert board["board_gsm"] == pytest.approx(1000)
    assert board["board_price_per_tonne"] == pytest.approx(794)


def test_full_bom_export_adds_costing_for_newer_box_items() -> None:
    repository = CsvRepository(PROJECT_DATA)
    bom = repository.load_bom_lines()
    item = repository.load_current_items().loc[
        lambda frame: frame["item_code"] == "BOX002/101/NPO/T0042/01/900G"
    ].iloc[0]
    result = repository.material_breakdown("BOX002/101/NPO/T0042/01/900G")
    summary = result["summary"]

    assert len(bom) == 9494
    assert bom["bom_code"].nunique() == 916
    assert summary["board_article_code"] == "4-15953"
    assert summary["board_price_per_tonne"] == pytest.approx(793)
    assert summary["board_cost_per_1000"] == pytest.approx(396.5)
    assert summary["other_components_cost_per_1000"] == pytest.approx(15.0376)
    assert summary["materials_cost_per_1000"] == pytest.approx(411.5376)
    assert summary["machine_hours_per_1000"] == pytest.approx(1.082598)
    assert item["materials_cost_per_1000"] == pytest.approx(
        summary["materials_cost_per_1000"]
    )


def test_materials_use_april_mill_price_and_bom_components() -> None:
    repository = CsvRepository(PROJECT_DATA)
    result = repository.material_breakdown("BOX001/101/LPB/1000G/1240P")
    summary = result["summary"]

    assert summary["board_article_code"] == "4-15614"
    assert summary["board_price_per_tonne"] == pytest.approx(794)
    assert summary["board_tonnes_per_1000"] == pytest.approx(0.596)
    assert summary["board_cost_per_1000"] == pytest.approx(473.224)
    assert summary["other_components_cost_per_1000"] == pytest.approx(15.0376)
    assert summary["materials_cost_per_1000"] == pytest.approx(488.2616)
    assert summary["machine_hours_per_1000"] == pytest.approx(0.375958)
    assert summary["machine_time_source"] == "BOM operation speeds"


def test_unmatched_board_falls_back_to_material_only_bom_value() -> None:
    repository = CsvRepository(PROJECT_DATA)
    result = repository.material_breakdown("BOX001/101/NPL/D9999/04/1000G")
    summary = result["summary"]

    assert "machine/labour removed" in summary["board_price_source"]
    assert summary["board_cost_per_1000"] == pytest.approx(499.73886)
    assert summary["board_cost_per_1000"] < 555.26122
    assert summary["machine_hours_per_1000"] == pytest.approx(0.478245)
    assert "rolled-child" in summary["machine_time_source"]


def test_new_item_materials_are_derived_without_a_typed_cost() -> None:
    repository = CsvRepository(PROJECT_DATA)
    result = repository.new_item_material_breakdown(
        "BRD001/101/LPB/1000G/BW",
        units_out=2,
        component_template_item_code="BOX001/101/LPB/1000G/1240P",
    )
    summary = result["summary"]

    assert summary["board_price_per_tonne"] == pytest.approx(794)
    assert summary["board_tonnes_per_1000"] == pytest.approx(0.596162)
    assert summary["board_cost_per_1000"] == pytest.approx(473.352628)
    assert summary["other_components_cost_per_1000"] == pytest.approx(15.0376)
    assert summary["machine_hours_per_1000"] == pytest.approx(0.375958)


def test_runtime_sessions_can_be_seen_forced_out_and_ended(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    repository.touch_session(
        {
            "session_id": "session-one",
            "username": "alice",
            "name": "Alice",
            "signed_in_at_utc": "2026-08-07T09:00:00+00:00",
            "last_activity_utc": "2026-08-07T09:05:00+00:00",
            "last_heartbeat_utc": "2026-08-07T09:05:00+00:00",
            "active_seconds": 240,
            "current_page": "Costing workflow",
            "force_logout": 0,
        }
    )
    repository.touch_session(
        {
            "session_id": "session-two",
            "username": "bob",
            "name": "Bob",
            "force_logout": 0,
        }
    )

    assert set(repository.load_sessions()["username"]) == {"alice", "bob"}
    assert not repository.session_forced_logout("session-two")
    repository.force_logout_session("session-two")
    assert repository.session_forced_logout("session-two")
    assert repository.touch_session(
        {
            "session_id": "session-two",
            "last_heartbeat_utc": "2026-08-07T09:06:00+00:00",
        }
    )

    repository.end_session("session-one")
    ended = repository.load_sessions().set_index("session_id").loc["session-one"]
    assert str(ended["ended_at_utc"]).strip()


def test_inactive_runtime_sessions_are_automatically_ended(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    repository.touch_session(
        {
            "session_id": "expired-session",
            "username": "old-user",
            "name": "Old User",
            "signed_in_at_utc": "2020-01-01T09:00:00+00:00",
            "last_activity_utc": "2020-01-01T09:05:00+00:00",
            "last_heartbeat_utc": "2020-01-01T09:05:00+00:00",
        }
    )

    assert repository.expire_inactive_sessions(60) == 1
    expired = repository.load_sessions().set_index("session_id").loc[
        "expired-session"
    ]
    assert str(expired["ended_at_utc"]).startswith("2020-01-01T10:05:00")
    assert repository.expire_inactive_sessions(60) == 0


def test_database_session_expiry_uses_a_python_datetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    assert repository.expire_inactive_sessions(60) == 1
    sql, params = cursor.executed[-1]
    assert "UPDATE public.app_sessions" in sql
    assert params[0] == 60
    assert isinstance(params[1], datetime)


def test_reference_csv_is_reused_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boards_path = tmp_path / "board_items.csv"
    pd.DataFrame([{"board_item_code": "BOARD-1"}]).to_csv(boards_path, index=False)
    repository = CsvRepository(tmp_path)
    real_read_csv = pd.read_csv
    calls = 0

    def counted_read_csv(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_read_csv(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", counted_read_csv)

    assert repository.load_board_items().iloc[0]["board_item_code"] == "BOARD-1"
    assert repository.load_board_items().iloc[0]["board_item_code"] == "BOARD-1"
    assert calls == 1

    pd.DataFrame([{"board_item_code": "BOARD-2"}]).to_csv(boards_path, index=False)
    assert repository.load_board_items().iloc[0]["board_item_code"] == "BOARD-2"
    assert calls == 2
