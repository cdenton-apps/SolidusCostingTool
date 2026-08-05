from __future__ import annotations

from pathlib import Path

import pytest

from src.transport import HaulierRateTable, TransportLookupError, match_rate_zone


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "haulier_rates.csv"


@pytest.fixture
def rate_table() -> HaulierRateTable:
    return HaulierRateTable(DATA_PATH)


def test_postcode_zone_ranges(rate_table: HaulierRateTable) -> None:
    zones = rate_table.available_zones
    assert match_rate_zone("DN16 1AA", zones) == "DN 15-25"
    assert match_rate_zone("YO19 4AA", zones) == "YO1-8,19,51,60"
    assert match_rate_zone("YO10 4AA", zones) == "YO (All Other)"
    assert match_rate_zone("PH14 7AA", zones) == "PH 1-7, 14"


def test_cheapest_bd_economy_rate(rate_table: HaulierRateTable) -> None:
    quotes = rate_table.quote_options(
        postcode="BD20 0AA", pallet_count=5, service="Economy"
    )
    assert quotes[0].vendor == "Joda"
    assert quotes[0].total_cost == pytest.approx(123)
    assert quotes[1].vendor == "McDowells"
    assert quotes[1].total_cost == pytest.approx(127)


def test_mcdowells_full_load_surcharge(rate_table: HaulierRateTable) -> None:
    quotes = rate_table.quote_options(
        postcode="BD20 0AA", pallet_count=26, service="Economy"
    )
    mcdowells = next(quote for quote in quotes if quote.vendor == "McDowells")
    assert mcdowells.base_cost == pytest.approx(133)
    assert mcdowells.full_load_surcharge == pytest.approx(40)
    assert mcdowells.total_cost == pytest.approx(173)


def test_multiple_loads_and_booking_surcharge(rate_table: HaulierRateTable) -> None:
    quotes = rate_table.quote_options(
        postcode="BD20 0AA", pallet_count=27, service="Economy", booking="AM/PM"
    )
    mcdowells = next(quote for quote in quotes if quote.vendor == "McDowells")
    assert mcdowells.load_count == 2
    assert mcdowells.booking_surcharge == pytest.approx(14)
    assert mcdowells.total_cost == pytest.approx(234)


def test_mtc_calloffs_cost_more_than_one_combined_delivery(
    rate_table: HaulierRateTable,
) -> None:
    combined = rate_table.quote_schedule(
        postcode="BD20 0AA",
        total_pallets=10,
        pallets_per_delivery=10,
        service="Economy",
    )
    calloffs = rate_table.quote_schedule(
        postcode="BD20 0AA",
        total_pallets=10,
        pallets_per_delivery=1,
        service="Economy",
    )

    assert combined[0].delivery_count == 1
    assert calloffs[0].delivery_count == 10
    assert calloffs[0].total_cost > combined[0].total_cost


def test_unavailable_vendor_is_omitted(rate_table: HaulierRateTable) -> None:
    quotes = rate_table.quote_options(
        postcode="LL38 1AA", pallet_count=10, service="Economy"
    )
    assert [quote.vendor for quote in quotes] == ["McDowells"]


def test_unknown_postcode_zone_is_rejected(rate_table: HaulierRateTable) -> None:
    with pytest.raises(TransportLookupError):
        rate_table.quote_options(
            postcode="ZE1 0AA", pallet_count=2, service="Economy"
        )
    with pytest.raises(TransportLookupError):
        rate_table.quote_options(
            postcode="PE31 4AA", pallet_count=2, service="Economy"
        )
