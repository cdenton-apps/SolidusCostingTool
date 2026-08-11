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
    "annual_volume_units": "Expected annual volume",
    "delivery_postcode": "Delivery postcode",
}


COMEX_FACTORS = {
    "comex_consistent_payer": ("Consistent Payer", -5.0),
    "comex_strategic_customer": ("Strategic Customer", -3.0),
    "comex_over_credit_limit": ("Over Credit Limit", 10.0),
    "comex_poor_payment_history": ("Poor Payment History", 5.0),
}

ANNUAL_VOLUME_ADJUSTMENTS = {
    "0 - 10,000": 15.0,
    "10,001 - 25,000": 10.0,
    "25,001 - 50,000": 5.0,
    "50,001 - 100,000": 0.0,
    "100,001 - 1,000,000": -10.0,
    "Over 1,000,000": -15.0,
}

DEFAULT_ANNUAL_VOLUME_BAND = "50,001 - 100,000"


def annual_volume_band_for_units(annual_volume_units: float) -> str:
    """Map an entered annual unit volume to the internal pricing band."""
    volume = max(0.0, float(annual_volume_units))
    if volume <= 10_000:
        return "0 - 10,000"
    if volume <= 25_000:
        return "10,001 - 25,000"
    if volume <= 50_000:
        return "25,001 - 50,000"
    if volume <= 100_000:
        return "50,001 - 100,000"
    if volume <= 1_000_000:
        return "100,001 - 1,000,000"
    return "Over 1,000,000"


def _number(values: dict[str, Any], key: str) -> float:
    try:
        return float(values.get(key, 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc


def _flag(values: dict[str, Any], key: str) -> bool:
    value = values.get(key, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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
        "annual_volume_units",
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
        if (
            _number(values, "pallet_holding_charge_per_pallet_per_week")
            < MIN_PALLET_HOLDING_CHARGE
        ):
            errors.append(
                f"Pallet holding charge must be at least "
                f"£{MIN_PALLET_HOLDING_CHARGE:.2f} per pallet per week."
            )
    return errors


def calculate_cost(values: dict[str, Any]) -> dict[str, float]:
    """Calculate the material-led pricing base per 1,000 units.

    Machine and labour values may still exist in the source BOM extract, but the
    pricing base excludes them. Transport is added at cost before spread.
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
    annual_volume_units = _number(values, "annual_volume_units")
    annual_volume_band = (
        annual_volume_band_for_units(annual_volume_units)
        if annual_volume_units > 0
        else str(
            values.get("annual_volume_band", DEFAULT_ANNUAL_VOLUME_BAND)
            or DEFAULT_ANNUAL_VOLUME_BAND
        )
    )
    annual_volume_adjustment = ANNUAL_VOLUME_ADJUSTMENTS.get(
        annual_volume_band, 0.0
    )
    comex_adjustment = sum(
        percent
        for key, (_, percent) in COMEX_FACTORS.items()
        if _flag(values, key)
    )
    # Commercial percentages are deliberately additive. They are applied once
    # to material cost only, never sequentially and never to delivery.
    total_material_adjustment = annual_volume_adjustment + comex_adjustment
    material_adjustment_value = materials * total_material_adjustment / 100
    material_base = materials + material_adjustment_value

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
        "annual_volume_adjustment_percent": round(annual_volume_adjustment, 4),
        "comex_adjustment_percent": round(comex_adjustment, 4),
        "total_material_adjustment_percent": round(total_material_adjustment, 4),
        "material_adjustment_value_per_1000": round(material_adjustment_value, 4),
        "adjusted_materials_cost_per_1000": round(material_base, 4),
        "material_base_per_1000": round(material_base, 4),
        "transport_cost_per_1000": round(transport_cost_per_1000, 4),
        "pricing_base_per_1000": round(pricing_base, 4),
        "pricing_base_per_item": round(pricing_base / 1_000, 5),
        "machine_hours_per_1000": round(machine_hours_per_1000, 6),
        "total_machine_hours": round(total_machine_hours, 4),
    }


def traffic_light_result(
    spread_per_machine_hour: float,
    spread_percent: float,
) -> dict[str, str]:
    """Return the commercial traffic-light result and a plain reason."""
    hourly = float(spread_per_machine_hour)
    margin = float(spread_percent)
    if hourly < 600 or margin < 25:
        failures: list[str] = []
        if hourly < 600:
            failures.append("spread per machine hour is below £600")
        if margin < 25:
            failures.append("spread is below 25%")
        return {"status": "red", "reason": " and ".join(failures)}
    if margin >= 30:
        return {
            "status": "green",
            "reason": "spread per machine hour is at least £600 and spread is at least 30%",
        }
    return {
        "status": "amber",
        "reason": "spread per machine hour is at least £600 and spread is between 25% and 30%",
    }


def operational_spread_metrics(
    spread_value_per_1000: float,
    order_quantity: float,
    machine_hours_per_1000: float,
) -> dict[str, float]:
    """Convert a supplied spread value into quote and hourly measures."""
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
MIN_PALLET_HOLDING_CHARGE = 1.75
