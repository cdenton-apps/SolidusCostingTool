from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

from src.repository import CsvRepository


PROJECT_DATA = Path(__file__).resolve().parents[1] / "data"


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
        user_username="connor",
        user_email="Connor@Example.com",
        user_name="Connor",
    )
    repository.save_costing(
        {"item_code": "THEIRS-001", "customer_name": "Other customer"},
        user_username="other",
        user_email="other@example.com",
        user_name="Other User",
    )

    mine = repository.load_user_history("connor@example.com")
    assert list(mine["item_code"]) == ["MINE-001"]
    assert set(mine["created_by"]) == {"Connor@Example.com"}
    assert set(mine["created_by_username"]) == {"connor"}


def test_saved_item_appears_in_catalog(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    repository.save_costing(
        {"item_code": "NEW-001", "description": "New item"},
        user_email="one@example.com",
        user_name="User One",
    )
    catalog = repository.load_catalog()
    assert catalog.iloc[0]["item_code"] == "NEW-001"
    assert catalog.iloc[0]["source_type"] == "Saved costing"


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
