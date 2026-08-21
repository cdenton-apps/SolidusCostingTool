from __future__ import annotations

from pathlib import Path

import pytest

from src.multi_item import allocate_transport_by_pallets, price_multi_item_transport
from src.transport import HaulierRateTable


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "haulier_rates.csv"


@pytest.fixture
def rate_table() -> HaulierRateTable:
    return HaulierRateTable(DATA_PATH)


def test_combined_transport_is_allocated_exactly_by_pallet_share() -> None:
    allocation = allocate_transport_by_pallets(100, [2, 1])

    assert allocation == [66.67, 33.33]
    assert sum(allocation) == pytest.approx(100)


def test_delivered_together_uses_26_pallet_loads(
    rate_table: HaulierRateTable,
) -> None:
    priced = price_multi_item_transport(
        rate_table,
        pallet_counts=[20, 15, 18],
        delivery_mode="Delivered together",
        postcode="BD20 0AA",
        service="Next Day",
        booking="AM/PM",
        fulfilment_type="MTO",
        pallets_per_delivery=0,
    )

    # 53 pallets = 26 + 26 + 1.
    assert priced["load_count"] == 3
    assert priced["delivery_count"] == 1
    assert sum(priced["line_costs"]) == pytest.approx(priced["total_cost"])


def test_delivered_separately_prices_each_item_as_its_own_movement(
    rate_table: HaulierRateTable,
) -> None:
    combined = price_multi_item_transport(
        rate_table,
        pallet_counts=[10, 10],
        delivery_mode="Delivered together",
        postcode="BD20 0AA",
        service="Next Day",
        booking="AM/PM",
        fulfilment_type="MTO",
        pallets_per_delivery=0,
    )
    separate = price_multi_item_transport(
        rate_table,
        pallet_counts=[10, 10],
        delivery_mode="Delivered separately",
        postcode="BD20 0AA",
        service="Next Day",
        booking="AM/PM",
        fulfilment_type="MTO",
        pallets_per_delivery=0,
    )

    assert separate["delivery_count"] == 2
    assert separate["total_cost"] > combined["total_cost"]


def test_delivered_separately_starts_another_trailer_over_26_pallets(
    rate_table: HaulierRateTable,
) -> None:
    priced = price_multi_item_transport(
        rate_table,
        pallet_counts=[27, 1],
        delivery_mode="Delivered separately",
        postcode="BD20 0AA",
        service="Next Day",
        booking="AM/PM",
        fulfilment_type="MTO",
        pallets_per_delivery=0,
    )

    # The first item needs 26 + 1 pallets; the second item is its own trailer.
    assert priced["load_count"] == 3
    assert priced["delivery_count"] == 2
