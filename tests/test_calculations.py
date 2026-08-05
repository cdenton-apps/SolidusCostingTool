from __future__ import annotations

import pytest

from src.calculations import (
    calculate_cost,
    price_from_spread_percent,
    spread_percent_from_price,
    validate_details,
)


@pytest.fixture
def valid_costing() -> dict:
    return {
        "customer_name": "Test customer",
        "item_code": "TEST-001",
        "description": "Test box",
        "material": "Solid board",
        "board_gsm": 400,
        "length_mm": 500,
        "width_mm": 400,
        "height_mm": 100,
        "net_mass_kg": 0.08,
        "pallet_quantity": 2_000,
        "order_quantity": 10_000,
        "materials_cost_per_1000": 70.4,
        "print_machine_cost_per_1000": 10,
        "die_cut_machine_cost_per_1000": 20,
        "fold_glue_machine_cost_per_1000": 5,
        "other_machine_cost_per_1000": 2,
        "labour_cost_per_1000": 15,
        "manual_adjustment_per_1000": 3,
        "fixed_tooling_cost": 100,
        "delivery_postcode": "BD20 0AA",
        "delivery_method": "Haulier",
        "transport_total": 250,
    }


def test_cost_breakdown(valid_costing: dict) -> None:
    result = calculate_cost(valid_costing)

    assert result["net_weight_kg_per_1000"] == pytest.approx(80)
    assert result["pallet_count"] == 5
    assert result["transport_cost_per_1000"] == pytest.approx(25)
    assert "tooling_cost_per_1000" not in result
    assert result["material_base_per_1000"] == pytest.approx(73.4)
    assert result["pricing_base_per_1000"] == pytest.approx(98.4)


def test_machine_and_labour_source_values_are_ignored(valid_costing: dict) -> None:
    result = calculate_cost(valid_costing)
    valid_costing.update(
        {
            "print_machine_cost_per_1000": 10_000,
            "die_cut_machine_cost_per_1000": 10_000,
            "fold_glue_machine_cost_per_1000": 10_000,
            "other_machine_cost_per_1000": 10_000,
            "labour_cost_per_1000": 10_000,
        }
    )
    assert calculate_cost(valid_costing) == result


def test_customer_collection_has_no_transport_cost(valid_costing: dict) -> None:
    valid_costing["delivery_method"] = "Customer collection"
    result = calculate_cost(valid_costing)
    assert result["transport_total"] == 0
    assert result["transport_cost_per_1000"] == 0
    assert result["pricing_base_per_1000"] == pytest.approx(73.4)


def test_spread_and_price_are_reversible() -> None:
    price = price_from_spread_percent(140.4, 30)
    spread = spread_percent_from_price(140.4, price["selling_price_per_1000"])
    assert price["spread_value_per_1000"] == pytest.approx(60.1714)
    assert price["selling_price_per_1000"] == pytest.approx(200.5714)
    assert spread["spread_percent"] == pytest.approx(30, abs=0.001)


def test_validation_blocks_missing_fields(valid_costing: dict) -> None:
    valid_costing["item_code"] = ""
    valid_costing["order_quantity"] = 0
    errors = validate_details(valid_costing)
    assert "Item code is required." in errors
    assert "Order quantity must be greater than zero." in errors
