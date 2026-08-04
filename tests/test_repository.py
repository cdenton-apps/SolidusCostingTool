from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.repository import CsvRepository


PROJECT_DATA = Path(__file__).resolve().parents[1] / "data"


def test_saves_append_only_revisions(tmp_path: Path) -> None:
    repository = CsvRepository(tmp_path)
    record = {
        "item_code": "ITEM-001",
        "description": "A test item",
        "customer_name": "Customer",
        "total_cost_per_1000": 100,
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
    item = items[items["item_code"] == "BOX001/101/LPB/1000G/1240P"].iloc[0]

    assert len(items) == 354
    assert int(items["bom_available"].sum()) == 179
    assert item["pallet_quantity"] == 1240
    assert item["materials_cost_per_1000"] == 488.26
    assert item["imported_machine_cost_per_1000"] == 66.92
    assert item["labour_cost_per_1000"] == 51.09
    assert item["imported_bom_total_per_1000"] == 606.27
