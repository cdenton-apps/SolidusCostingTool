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
    """Return human-readable validation errors for the order stage."""
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

    fulfilment_type = str(values.get("fulfilment_type", "MTO") or "MTO").upper()
    if fulfilment_type not in {"MTO", "MTC"}:
        errors.append("Fulfilment type must be MTO or MTC.")
    if fulfilment_type == "MTC":
        if _number(values, "agreement_term_months") <= 0:
            errors.append("Agreement term must be greater than zero months.")
        if _number(values, "delivery_pallets_per_calloff") <= 0:
            errors.append("Pallets per delivery must be greater than zero.")
        if _number(values, "pallet_holding_charge_per_pallet_per_week") < 0:
            errors.append("Pallet holding charge cannot be negative.")
    return errors


def calculate_cost(values: dict[str, Any]) -> dict[str, float]:
    """Calculate the material-led pricing base per 1,000 units.

    Machine and labour values may still exist in the source BOM extract, but the
    commercial model deliberately excludes them. Manual adjustments and transport
    are treated as pass-throughs before the spread is applied.
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
    material_base = materials + manual_adjustment

    delivery_method = str(values.get("delivery_method", "Haulier"))
    transport_total = (
        _number(values, "transport_total") if delivery_method == "Haulier" else 0.0
    )
    transport_cost_per_1000 = transport_total / order_in_thousands
    pricing_base = material_base + transport_cost_per_1000
    machine_hours_per_1000 = _number(values, "machine_hours_per_1000")
    total_machine_hours = machine_hours_per_1000 * order_in_thousands

    return {
        "net_weight_kg_per_1000": round(net_weight_kg_per_1000, 4),
        "pallet_count": float(pallet_count),
        "transport_total": round(transport_total, 4),
        "materials_cost_per_1000": round(materials, 4),
        "manual_adjustment_per_1000": round(manual_adjustment, 4),
        "material_base_per_1000": round(material_base, 4),
        "transport_cost_per_1000": round(transport_cost_per_1000, 4),
        "pricing_base_per_1000": round(pricing_base, 4),
        "pricing_base_per_item": round(pricing_base / 1_000, 5),
        "machine_hours_per_1000": round(machine_hours_per_1000, 6),
        "total_machine_hours": round(total_machine_hours, 4),
    }


def operational_spread_metrics(
    spread_value_per_1000: float,
    order_quantity: float,
    machine_hours_per_1000: float,
) -> dict[str, float]:
    """Return operational spread measures without changing the pricing base."""
    order_in_thousands = max(0.0, float(order_quantity)) / 1_000
    total_spread_value = float(spread_value_per_1000) * order_in_thousands
    total_machine_hours = max(0.0, float(machine_hours_per_1000)) * order_in_thousands
    spread_per_machine_hour = (
        total_spread_value / total_machine_hours if total_machine_hours > 0 else 0.0
    )
    return {
        "total_spread_value": round(total_spread_value, 4),
        "total_machine_hours": round(total_machine_hours, 4),
        "spread_per_machine_hour": round(spread_per_machine_hour, 4),
    }


def price_from_spread_percent(
    pricing_base_per_1000: float,
    spread_percent: float,
) -> dict[str, float]:
    """Return selling price for a gross spread percentage.

    Spread is the share of selling price left after the pricing base:
    ``(selling price - pricing base) / selling price``.
    """
    if pricing_base_per_1000 < 0:
        raise ValueError("Pricing base cannot be negative.")
    if spread_percent >= 100:
        raise ValueError("Spread percentage must be less than 100%.")
    selling_price = pricing_base_per_1000 / (1 - spread_percent / 100)
    spread_value_per_1000 = selling_price - pricing_base_per_1000
    return {
        "spread_percent": round(spread_percent, 4),
        "spread_value_per_1000": round(spread_value_per_1000, 4),
        "selling_price_per_1000": round(selling_price, 4),
        "selling_price_per_item": round(selling_price / 1_000, 5),
    }


def spread_percent_from_price(
    pricing_base_per_1000: float,
    selling_price: float,
) -> dict[str, float]:
    """Return the achieved gross spread percentage for a selling price."""
    if pricing_base_per_1000 < 0:
        raise ValueError("Pricing base cannot be negative.")
    if selling_price <= 0:
        raise ValueError("Selling price must be greater than zero.")
    spread_value_per_1000 = selling_price - pricing_base_per_1000
    spread_percent = spread_value_per_1000 / selling_price * 100
    return {
        "spread_percent": round(spread_percent, 4),
        "spread_value_per_1000": round(spread_value_per_1000, 4),
        "selling_price_per_1000": round(selling_price, 4),
        "selling_price_per_item": round(selling_price / 1_000, 5),
    }
