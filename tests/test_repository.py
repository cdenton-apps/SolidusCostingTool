from __future__ import annotations

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
        "target_spread_per_tonne": 250,
        "selling_price_per_1000": 150,
    }

    first = repository.save_costing(
        record, user_email="one@example.com", user_name="User One"
    )
    second = repository.save_costing(
        {**record, "selling_price_per_1000": 160},
        user_email="two@example.com",
        user_name="User Two",
    )
    history = repository.load_history()

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert len(history) == 2
    assert list(history["created_by_name"]) == ["User One", "User Two"]
    assert list(pd.to_numeric(history["selling_price_per_1000"])) == [150, 160]


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

    assert len(items) == 354
    assert len(boards) == 986
    assert len(prices) == 1163
    assert pd.to_numeric(boards["price_per_tonne"], errors="coerce").notna().sum() == 579
    assert int(items["bom_available"].sum()) == 179
    assert item["pallet_quantity"] == 1240
    assert item["materials_cost_per_1000"] == pytest.approx(488.2616)
    assert item["imported_machine_cost_per_1000"] == 66.92
    assert item["labour_cost_per_1000"] == 51.09
    assert item["imported_bom_total_per_1000"] == 606.27


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


def test_unmatched_board_falls_back_to_material_only_bom_value() -> None:
    repository = CsvRepository(PROJECT_DATA)
    result = repository.material_breakdown("BOX001/101/NPL/D9999/04/1000G")
    summary = result["summary"]

    assert "machine/labour removed" in summary["board_price_source"]
    assert summary["board_cost_per_1000"] == pytest.approx(499.73886)
    assert summary["board_cost_per_1000"] < 555.26122


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
