from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.repository import (
    CsvRepository,
    board_fit_layout,
    board_material_spec,
    flat_net_dimensions,
)


PROJECT_DATA = Path(__file__).resolve().parents[1] / "data"


def test_board_fit_uses_the_complete_net_and_is_not_capped_at_two_up() -> None:
    layout = board_fit_layout(200, 100, 470, 470)

    assert layout["units"] == 8
    assert layout["across"] * layout["down"] == 8
    assert board_fit_layout(451, 100, 470, 470)["units"] == 0


def test_flat_net_estimate_includes_every_side_wall() -> None:
    assert flat_net_dimensions(574, 376, 149) == pytest.approx((872, 674))


def test_board_material_is_derived_from_the_board_description() -> None:
    assert (
        board_material_spec("BOARD1360X876/1000GSM/KL/TKL.WPE")
        == "KL/TKL.WPE"
    )
    assert (
        board_material_spec("BOARD 1154 x 918/WHTSTWPE/BRTKLCPE/1050GSM")
        == "WHTSTWPE/BRTKLCPE"
    )


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
        if "last_failed_at_utc" in self.last_sql:
            return {
                "failed_attempts": 5,
                "locked_until_utc": datetime.now(timezone.utc) + timedelta(minutes=15),
            }
        if "COALESCE(MAX(revision)" in self.last_sql:
            return {"revision": 1}
        if "AS quote_number" in self.last_sql:
            return {"quote_number": 1000}
        if "AS quote_revision" in self.last_sql:
            return {"quote_revision": 2}
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
    assert first["quote_reference"] == "1000-1"
    assert second["quote_reference"] == "1001-1"
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
    assert saved["quote_reference"] == "1000-1"
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


def test_login_security_reports_a_temporary_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    status = repository.app_user_login_security("locked-user")

    assert status["failed_attempts"] == 5
    assert status["is_locked"] is True
    assert "interval '15 minutes'" in cursor.last_sql


def test_failed_login_is_audited_and_can_trigger_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    status = repository.record_login_failure("alice")

    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert status["is_locked"] is True
    assert "login_failed" in sql
    assert any(
        params and "login_locked" in params
        for _, params in cursor.executed
    )


def test_admin_unlock_is_written_to_the_existing_audit_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    repository.unlock_app_user("alice", actor_username="admin")

    sql, params = cursor.executed[-1]
    assert "INSERT INTO public.app_audit_log" in sql
    assert params[1:3] == ("login_unlocked", "alice")


def test_password_change_invalidates_existing_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    repository.change_app_user_password(
        "alice",
        "pbkdf2_sha256$600000$salt$digest",
        actor_username="alice",
    )

    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "session_version = session_version + 1" in sql
    assert "UPDATE public.app_sessions SET force_logout = true" in sql


def test_successful_login_updates_user_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CsvRepository(
        tmp_path,
        database_url="postgresql://example.invalid/neondb",
    )
    cursor = _FakeCursor()
    monkeypatch.setattr(repository, "_connect", lambda: _FakeConnection(cursor))

    repository.record_user_login("alice")

    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "last_login_at_utc = now()" in sql
    assert "login_success" in sql


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
    assert len({item["quote_reference"] for item in saved}) == 6
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
            "catalogue_product": True,
        },
        user_email="one@example.com",
        user_name="User One",
    )
    catalog = repository.load_catalog()
    assert catalog.iloc[0]["item_code"] == "NEW-001"
    assert catalog.iloc[0]["source_type"] == "Saved costing"


def test_saved_quote_does_not_replace_stock_catalogue_product(tmp_path: Path) -> None:
    for source in PROJECT_DATA.glob("*.csv"):
        if source.name not in {"saved_costings.csv", "active_sessions.csv"}:
            (tmp_path / source.name).write_bytes(source.read_bytes())
    compressed_bom = PROJECT_DATA / "bom_costs.csv.gz"
    (tmp_path / compressed_bom.name).write_bytes(compressed_bom.read_bytes())

    repository = CsvRepository(tmp_path)
    before = repository.load_catalog()
    master = before.iloc[0].to_dict()
    item_code = str(master["item_code"])
    master_customer = str(master.get("customer_name", "") or "")
    repository.save_costing(
        {
            **master,
            "customer_name": "A quotation-specific customer",
            "order_quantity": 99_000,
            "catalogue_product": False,
        },
        user_username="alice",
        user_email="alice@example.com",
        user_name="Alice",
    )

    after = repository.load_catalog()
    selected = after.loc[after["item_code"].eq(item_code)].iloc[0]
    assert selected["source_type"] == "Stock list"
    assert str(selected.get("customer_name", "") or "") == master_customer
    assert float(selected.get("order_quantity", 0) or 0) != 99_000


def test_catalog_hides_stock_items_without_a_costing_bom() -> None:
    repository = CsvRepository(PROJECT_DATA)
    catalog = repository.load_catalog()
    stock_items = catalog[catalog["source_type"] == "Stock list"]

    assert len(stock_items) == 594
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

    assert len(items) == 1313
    assert len(boards) == 999
    assert len(prices) == 1163
    assert pd.to_numeric(boards["price_per_tonne"], errors="coerce").notna().sum() == 585
    assert int(items["bom_available"].sum()) == 594
    assert "BOXT701/102/YPT/1000G/1800P" in set(items["item_code"])
    assert item["pallet_quantity"] == 1240
    assert item["materials_cost_per_1000"] == pytest.approx(488.2616)
    assert item["imported_machine_cost_per_1000"] == 66.92
    assert item["labour_cost_per_1000"] == 51.09
    assert item["imported_bom_total_per_1000"] == 606.27
    assert item["machine_hours_per_1000"] == pytest.approx(0.375958)
    assert item["material"] == "BK/TKL.WPE"


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


def test_unpriced_board_lookup_returns_material_and_keeps_entered_article() -> None:
    repository = CsvRepository(PROJECT_DATA)

    board = repository.find_board_by_code("4-17237/", manufacturing_site="101")

    assert board is not None
    assert board["board_item_code"] == "BRD001/102/YPL/1000G/WW(2)"
    assert board["board_code"] == "4-17237"
    assert board["board_material_spec"] == "WT/TKL.WPE"
    assert board["board_price_per_tonne"] == 0


def test_full_bom_export_adds_costing_for_newer_box_items() -> None:
    repository = CsvRepository(PROJECT_DATA)
    bom = repository.load_bom_lines()
    item = repository.load_current_items().loc[
        lambda frame: frame["item_code"] == "BOX002/101/NPO/T0042/01/900G"
    ].iloc[0]
    result = repository.material_breakdown("BOX002/101/NPO/T0042/01/900G")
    summary = result["summary"]

    assert len(bom) == 9698
    assert bom["bom_code"].nunique() == 938
    assert summary["board_article_code"] == "4-15953"
    assert summary["board_price_per_tonne"] == pytest.approx(793)
    assert summary["board_cost_per_1000"] == pytest.approx(396.5)
    assert "Plain board from printed BOM component" in summary["board_price_source"]
    assert summary["board_material_spec"] == "WTL/TKL.WPE"
    assert summary["other_components_cost_per_1000"] == pytest.approx(15.3468)
    assert summary["materials_cost_per_1000"] == pytest.approx(411.8468)
    assert summary["machine_hours_per_1000"] == pytest.approx(0.853854)
    assert item["board_item_code"] == "BRD002/101/YPB/900G/WW"
    assert item["material"] == "WTL/TKL.WPE"


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


def test_printed_board_resolves_to_plain_child_board_and_complete_bom() -> None:
    repository = CsvRepository(PROJECT_DATA)
    result = repository.material_breakdown("BOX001/101/NPL/D9999/04/1000G")
    summary = result["summary"]

    assert summary["board_item_code"] == "BRD001/101/NPL/1000G/BB"
    assert summary["board_material_spec"] == "BK/BK"
    assert "Plain board from printed BOM component" in summary["board_price_source"]
    assert summary["board_price_per_tonne"] == pytest.approx(780)
    assert summary["board_cost_per_1000"] == pytest.approx(486.564)
    assert summary["other_components_cost_per_1000"] == pytest.approx(20.0629)
    print_components = result["lines"].loc[
        lambda frame: frame["component_type"].eq("Print-route component")
    ]
    assert set(print_components["component_code"]) == {
        "0-PALLETS/101/Std1000x1200",
        "0-TOPSHEET/101/1400mmx50UMS",
        "BLUE 295 U 8% HD",
    }
    assert summary["machine_hours_per_1000"] == pytest.approx(0.478245)
    assert "rolled-child" in summary["machine_time_source"]


def test_new_item_materials_are_derived_without_a_typed_cost() -> None:
    repository = CsvRepository(PROJECT_DATA)
    template_lines = repository.material_breakdown(
        "BOX001/101/LPB/1000G/1240P"
    )["lines"]
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
    assert summary["board_material_spec"] == "BK/TKL.WPE"

    expected_components = (
        template_lines.loc[
            template_lines["component_type"].eq("Other component"),
            ["component_code", "quantity", "unit_of_measure", "cost_per_1000"],
        ]
        .sort_values("component_code")
        .reset_index(drop=True)
    )
    actual_components = (
        result["lines"].loc[
            result["lines"]["component_type"].eq("Other component"),
            ["component_code", "quantity", "unit_of_measure", "cost_per_1000"],
        ]
        .sort_values("component_code")
        .reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(actual_components, expected_components)


def test_new_printed_item_uses_complete_print_route_bom_at_selected_x_up() -> None:
    repository = CsvRepository(PROJECT_DATA)

    with pytest.raises(ValueError, match="Enter a board price per tonne"):
        repository.new_item_material_breakdown(
            "BRD001/102/YPL/1000G/WW(2)",
            units_out=2,
            component_template_item_code="BOX001/101/YPL/M0115/02/1000G",
            number_of_colours=901,
        )

    result = repository.new_item_material_breakdown(
        "BRD001/102/YPL/1000G/WW(2)",
        units_out=2,
        component_template_item_code="BOX001/101/YPL/M0115/02/1000G",
        board_price_per_tonne=763,
        number_of_colours=901,
    )
    summary = result["summary"]
    print_lines = result["lines"].loc[
        lambda frame: frame["component_type"].eq("Print-route component")
    ]

    assert summary["print_operations_included"] == 1
    assert summary["new_board_price_per_tonne"] == pytest.approx(763)
    assert summary["other_components_cost_per_1000"] == pytest.approx(17.0883)
    assert set(print_lines["component_code"]) == {
        "0-FACTORY/102/STRETCHWRAP",
        "PROCESS BLACK U",
        "PROCESS CYAN U",
        "PROCESS MAGENTA U",
        "PROCESS YELLOW C",
        "0-VARNISH-HD",
    }
    assert print_lines["source"].str.contains("2-up", regex=False).all()


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
