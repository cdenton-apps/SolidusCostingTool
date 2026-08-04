from __future__ import annotations

import math
from typing import Any


REQUIRED_FIELDS = {
    "customer_name": "Customer",
    "item_code": "Item code",
    "description": "Description",
    "material": "Material",
    "board_gsm": "GSM",
    "blank_length_mm": "Blank length",
    "blank_width_mm": "Blank width",
    "pallet_quantity": "Pallet quantity",
    "order_quantity": "Order quantity",
    "material_cost_per_tonne": "Material cost per tonne",
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
        "blank_length_mm",
        "blank_width_mm",
        "pallet_quantity",
        "order_quantity",
        "material_cost_per_tonne",
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

    waste = _number(values, "waste_percent")
    if not 0 <= waste < 100:
        errors.append("Waste must be between 0% and 99.99%.")
    return errors


def calculate_cost(values: dict[str, Any]) -> dict[str, float]:
    """Calculate a transparent cost breakdown, expressed per 1,000 units."""
    errors = validate_details(values)
    if errors:
        raise ValueError(" ".join(errors))

    length_m = _number(values, "blank_length_mm") / 1_000
    width_m = _number(values, "blank_width_mm") / 1_000
    gsm = _number(values, "board_gsm")
    waste_multiplier = 1 + (_number(values, "waste_percent") / 100)

    # Area (m²) × gsm gives grams per item; for 1,000 items the same
    # numeric value is kilograms.
    net_weight_kg_per_1000 = length_m * width_m * gsm
    gross_weight_kg_per_1000 = net_weight_kg_per_1000 * waste_multiplier
    material_cost_per_1000 = (
        gross_weight_kg_per_1000
        / 1_000
        * _number(values, "material_cost_per_tonne")
    )

    order_quantity = _number(values, "order_quantity")
    order_in_thousands = order_quantity / 1_000
    pallet_quantity = _number(values, "pallet_quantity")
    pallet_count = math.ceil(order_quantity / pallet_quantity)

    delivery_method = str(values.get("delivery_method", "Haulier"))
    rate_per_pallet = _number(values, "transport_rate_per_pallet")
    transport_total = (
        pallet_count * rate_per_pallet if delivery_method == "Haulier" else 0.0
    )
    transport_cost_per_1000 = transport_total / order_in_thousands
    tooling_cost_per_1000 = _number(values, "fixed_tooling_cost") / order_in_thousands

    components = {
        "material_cost_per_1000": material_cost_per_1000,
        "bom_cost_per_1000": _number(values, "bom_cost_per_1000"),
        "print_cost_per_1000": _number(values, "print_cost_per_1000"),
        "conversion_cost_per_1000": _number(values, "conversion_cost_per_1000"),
        "packing_cost_per_1000": _number(values, "packing_cost_per_1000"),
        "tooling_cost_per_1000": tooling_cost_per_1000,
        "transport_cost_per_1000": transport_cost_per_1000,
    }
    total_cost = sum(components.values())

    return {
        "net_weight_kg_per_1000": round(net_weight_kg_per_1000, 4),
        "gross_weight_kg_per_1000": round(gross_weight_kg_per_1000, 4),
        "pallet_count": float(pallet_count),
        "transport_total": round(transport_total, 4),
        **{key: round(value, 4) for key, value in components.items()},
        "total_cost_per_1000": round(total_cost, 4),
        "cost_per_item": round(total_cost / 1_000, 6),
    }


def price_from_margin(cost_per_1000: float, margin_percent: float) -> dict[str, float]:
    if cost_per_1000 < 0:
        raise ValueError("Cost cannot be negative.")
    if not 0 <= margin_percent < 100:
        raise ValueError("Margin must be between 0% and 99.99%.")
    selling_price = cost_per_1000 / (1 - margin_percent / 100)
    return {
        "preferred_margin_percent": round(margin_percent, 4),
        "selling_price_per_1000": round(selling_price, 4),
        "selling_price_per_item": round(selling_price / 1_000, 6),
    }


def margin_from_price(cost_per_1000: float, selling_price: float) -> dict[str, float]:
    if selling_price <= 0:
        raise ValueError("Selling price must be greater than zero.")
    margin_percent = ((selling_price - cost_per_1000) / selling_price) * 100
    return {
        "preferred_margin_percent": round(margin_percent, 4),
        "selling_price_per_1000": round(selling_price, 4),
        "selling_price_per_item": round(selling_price / 1_000, 6),
    }

