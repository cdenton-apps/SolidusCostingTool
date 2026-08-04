from __future__ import annotations

import math
from typing import Any


REQUIRED_FIELDS = {
    "customer_name": "Customer",
    "item_code": "Item code",
    "description": "Description",
    "material": "Material",
    "board_gsm": "Grade / GSM",
    "length_mm": "Length",
    "width_mm": "Width",
    "height_mm": "Height",
    "pallet_quantity": "Pallet quantity",
    "order_quantity": "Order quantity",
    "delivery_postcode": "Delivery postcode",
}


def _number(values: dict[str, Any], key: str) -> float:
    try:
        return float(values.get(key, 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc


def validate_details(values: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors for the specification stage."""
    errors: list[str] = []
    numeric_required = {
        "board_gsm",
        "length_mm",
        "width_mm",
        "height_mm",
        "pallet_quantity",
        "order_quantity",
    }

    for key, label in REQUIRED_FIELDS.items():
        value = values.get(key)
        if key in numeric_required:
            try:
                if float(value or 0) <= 0:
                    errors.append(f"{label} must be greater than zero.")
            except (TypeError, ValueError):
                errors.append(f"{label} must be a number.")
        elif not str(value or "").strip():
            errors.append(f"{label} is required.")
    return errors


def calculate_cost(values: dict[str, Any]) -> dict[str, float]:
    """Calculate the material-led pricing base per 1,000 units.

    Machine and labour values may still exist in the source BOM extract, but the
    commercial model deliberately excludes them. Tooling, manual adjustments and
    transport are treated as pass-throughs before the spread is applied.
    """
    errors = validate_details(values)
    if errors:
        raise ValueError(" ".join(errors))

    order_quantity = _number(values, "order_quantity")
    order_in_thousands = order_quantity / 1_000
    pallet_quantity = _number(values, "pallet_quantity")
    pallet_count = math.ceil(order_quantity / pallet_quantity)

    net_mass_kg = _number(values, "net_mass_kg")
    if net_mass_kg > 0:
        net_weight_kg_per_1000 = net_mass_kg * 1_000
    else:
        # A fallback estimate for brand-new items without an imported net mass.
        net_weight_kg_per_1000 = (
            _number(values, "length_mm")
            / 1_000
            * (_number(values, "width_mm") / 1_000)
            * _number(values, "board_gsm")
        )

    materials = _number(values, "materials_cost_per_1000")
    manual_adjustment = _number(values, "manual_adjustment_per_1000")
    tooling_cost_per_1000 = _number(values, "fixed_tooling_cost") / order_in_thousands
    material_base = materials + manual_adjustment + tooling_cost_per_1000

    delivery_method = str(values.get("delivery_method", "Haulier"))
    transport_total = (
        _number(values, "transport_total") if delivery_method == "Haulier" else 0.0
    )
    transport_cost_per_1000 = transport_total / order_in_thousands
    pricing_base = material_base + transport_cost_per_1000

    return {
        "net_weight_kg_per_1000": round(net_weight_kg_per_1000, 4),
        "pallet_count": float(pallet_count),
        "transport_total": round(transport_total, 4),
        "materials_cost_per_1000": round(materials, 4),
        "manual_adjustment_per_1000": round(manual_adjustment, 4),
        "tooling_cost_per_1000": round(tooling_cost_per_1000, 4),
        "material_base_per_1000": round(material_base, 4),
        "transport_cost_per_1000": round(transport_cost_per_1000, 4),
        "pricing_base_per_1000": round(pricing_base, 4),
        "pricing_base_per_item": round(pricing_base / 1_000, 6),
    }


def price_from_spread(
    pricing_base_per_1000: float,
    net_weight_kg_per_1000: float,
    target_spread_per_tonne: float,
) -> dict[str, float]:
    """Return selling price for a target spread expressed in pounds per tonne."""
    if pricing_base_per_1000 < 0:
        raise ValueError("Pricing base cannot be negative.")
    if net_weight_kg_per_1000 <= 0:
        raise ValueError("Net weight must be greater than zero to calculate spread.")
    if target_spread_per_tonne < 0:
        raise ValueError("Target spread cannot be negative.")
    spread_value_per_1000 = (
        target_spread_per_tonne * net_weight_kg_per_1000 / 1_000
    )
    selling_price = pricing_base_per_1000 + spread_value_per_1000
    return {
        "target_spread_per_tonne": round(target_spread_per_tonne, 4),
        "spread_value_per_1000": round(spread_value_per_1000, 4),
        "selling_price_per_1000": round(selling_price, 4),
        "selling_price_per_item": round(selling_price / 1_000, 6),
    }


def spread_from_price(
    pricing_base_per_1000: float,
    net_weight_kg_per_1000: float,
    selling_price: float,
) -> dict[str, float]:
    """Return achieved pounds-per-tonne spread for a selected selling price."""
    if pricing_base_per_1000 < 0:
        raise ValueError("Pricing base cannot be negative.")
    if net_weight_kg_per_1000 <= 0:
        raise ValueError("Net weight must be greater than zero to calculate spread.")
    if selling_price <= 0:
        raise ValueError("Selling price must be greater than zero.")
    spread_value_per_1000 = selling_price - pricing_base_per_1000
    spread_per_tonne = spread_value_per_1000 / net_weight_kg_per_1000 * 1_000
    return {
        "target_spread_per_tonne": round(spread_per_tonne, 4),
        "spread_value_per_1000": round(spread_value_per_1000, 4),
        "selling_price_per_1000": round(selling_price, 4),
        "selling_price_per_item": round(selling_price / 1_000, 6),
    }
