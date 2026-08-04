from __future__ import annotations

from pathlib import Path

import pytest

from src.calculations import calculate_cost
from src.repository import CsvRepository
from src.transport import HaulierRateTable


DATA_PATH = Path(__file__).resolve().parents[1] / "data"


def test_imported_material_and_haulier_rate_roll_into_pricing_base() -> None:
    repository = CsvRepository(DATA_PATH)
    rate_table = HaulierRateTable(repository.haulier_path)
    item = repository.load_current_items().loc[
        lambda frame: frame["item_code"] == "BOX001/101/LPB/1000G/1240P"
    ].iloc[0].to_dict()
    item.update(
        {
            "customer_name": "Integration test customer",
            "order_quantity": 10_000,
            "delivery_postcode": "BD20 0AA",
            "delivery_method": "Haulier",
        }
    )
    quotes = rate_table.quote_options(
        postcode="BD20 0AA", pallet_count=9, service="Economy"
    )
    item["transport_total"] = quotes[0].total_cost

    result = calculate_cost(item)

    assert quotes[0].vendor == "McDowells"
    assert quotes[0].total_cost == pytest.approx(133)
    assert result["material_base_per_1000"] == pytest.approx(488.2616)
    assert result["transport_cost_per_1000"] == pytest.approx(13.3)
    assert result["pricing_base_per_1000"] == pytest.approx(501.5616)
