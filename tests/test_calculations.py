from __future__ import annotations

import pytest

from src.calculations import (
    calculate_cost,
    margin_from_price,
    price_from_margin,
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
        "blank_length_mm": 500,
        "blank_width_mm": 400,
        "pallet_quantity": 2_000,
        "order_quantity": 10_000,
        "material_cost_per_tonne": 800,
        "bom_cost_per_1000": 0,
        "print_cost_per_1000": 10,
        "conversion_cost_per_1000": 20,
        "packing_cost_per_1000": 5,
        "fixed_tooling_cost": 100,
        "waste_percent": 10,
        "delivery_postcode": "BD20 0AA",
        "delivery_method": "Haulier",
        "transport_rate_per_pallet": 50,
    }


def test_cost_breakdown(valid_costing: dict) -> None:
    result = calculate_cost(valid_costing)

    assert result["net_weight_kg_per_1000"] == pytest.approx(80)
    assert result["gross_weight_kg_per_1000"] == pytest.approx(88)
    assert result["material_cost_per_1000"] == pytest.approx(70.4)
    assert result["pallet_count"] == 5
    assert result["transport_cost_per_1000"] == pytest.approx(25)
    assert result["tooling_cost_per_1000"] == pytest.approx(10)
    assert result["total_cost_per_1000"] == pytest.approx(140.4)


def test_customer_collection_has_no_transport_cost(valid_costing: dict) -> None:
    valid_costing["delivery_method"] = "Customer collection"
    result = calculate_cost(valid_costing)
    assert result["transport_total"] == 0
    assert result["transport_cost_per_1000"] == 0


def test_margin_and_price_are_reversible() -> None:
    price = price_from_margin(140.4, 30)
    margin = margin_from_price(140.4, price["selling_price_per_1000"])
    assert price["selling_price_per_1000"] == pytest.approx(200.5714)
    assert margin["preferred_margin_percent"] == pytest.approx(30, abs=0.001)


def test_validation_blocks_missing_fields(valid_costing: dict) -> None:
    valid_costing["item_code"] = ""
    valid_costing["order_quantity"] = 0
    errors = validate_details(valid_costing)
    assert "Item code is required." in errors
    assert "Order quantity must be greater than zero." in errors

