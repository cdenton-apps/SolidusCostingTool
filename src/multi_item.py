from __future__ import annotations

import math
from typing import Any

from src.transport import HaulierRateTable, TransportLookupError


MULTI_DELIVERY_MODES = ["Delivered together", "Delivered separately"]


def allocate_transport_by_pallets(
    total_cost: float,
    pallet_counts: list[int],
) -> list[float]:
    """Allocate a combined transport charge by pallet share, exact to the penny."""
    counts = [max(0, int(value)) for value in pallet_counts]
    total_pallets = sum(counts)
    if total_pallets <= 0:
        raise ValueError("At least one pallet is required to allocate transport.")
    total_pence = int(round(float(total_cost) * 100))
    raw_pence = [total_pence * count / total_pallets for count in counts]
    allocated = [math.floor(value) for value in raw_pence]
    remaining = total_pence - sum(allocated)
    remainders = sorted(
        range(len(counts)),
        key=lambda index: (raw_pence[index] - allocated[index], counts[index]),
        reverse=True,
    )
    for index in remainders[:remaining]:
        allocated[index] += 1
    return [value / 100 for value in allocated]


def _chosen_quote(options: list[Any], vendor_preference: str) -> Any:
    if not options:
        raise TransportLookupError("No transport option is available.")
    preference = str(vendor_preference or "Highest available").strip().casefold()
    if preference == "highest available":
        return max(options, key=lambda option: float(option.total_cost))
    if preference == "cheapest available":
        return min(options, key=lambda option: float(option.total_cost))
    for option in options:
        if str(option.vendor).casefold() == preference:
            return option
    raise TransportLookupError(
        f"{vendor_preference} does not have a complete rate for this quotation."
    )


def price_multi_item_transport(
    rate_table: HaulierRateTable,
    *,
    pallet_counts: list[int],
    delivery_mode: str,
    postcode: str,
    service: str,
    booking: str,
    fulfilment_type: str,
    pallets_per_delivery: int,
    vendor_preference: str = "Highest available",
) -> dict[str, Any]:
    """Price combined or separate multi-item movements with 26-pallet loads."""
    counts = [max(0, int(value)) for value in pallet_counts]
    if not counts or any(value <= 0 for value in counts):
        raise TransportLookupError("Every quotation item must produce at least one pallet.")
    if delivery_mode not in MULTI_DELIVERY_MODES:
        raise TransportLookupError(f"Unknown multi-item delivery mode: {delivery_mode}.")
    mtc = str(fulfilment_type or "MTO").upper() == "MTC"

    if delivery_mode == "Delivered together":
        total_pallets = sum(counts)
        planned_size = (
            min(max(1, int(pallets_per_delivery)), total_pallets)
            if mtc
            else total_pallets
        )
        chosen = _chosen_quote(
            rate_table.quote_schedule(
                postcode=postcode,
                total_pallets=total_pallets,
                pallets_per_delivery=planned_size,
                service=service,
                booking=booking,
            ),
            vendor_preference,
        )
        return {
            "delivery_mode": delivery_mode,
            "total_cost": chosen.total_cost,
            "line_costs": allocate_transport_by_pallets(chosen.total_cost, counts),
            "load_count": chosen.load_count,
            "delivery_count": chosen.delivery_count,
            "vendors": [chosen.vendor],
            "rate_zone": chosen.rate_zone,
        }

    line_costs: list[float] = []
    load_count = 0
    delivery_count = 0
    vendors: list[str] = []
    rate_zone = ""
    for pallet_count in counts:
        planned_size = (
            min(max(1, int(pallets_per_delivery)), pallet_count)
            if mtc
            else pallet_count
        )
        chosen = _chosen_quote(
            rate_table.quote_schedule(
                postcode=postcode,
                total_pallets=pallet_count,
                pallets_per_delivery=planned_size,
                service=service,
                booking=booking,
            ),
            vendor_preference,
        )
        line_costs.append(chosen.total_cost)
        load_count += int(chosen.load_count)
        delivery_count += int(chosen.delivery_count)
        vendors.append(chosen.vendor)
        rate_zone = rate_zone or chosen.rate_zone
    return {
        "delivery_mode": delivery_mode,
        "total_cost": round(sum(line_costs), 2),
        "line_costs": line_costs,
        "load_count": load_count,
        "delivery_count": delivery_count,
        "vendors": vendors,
        "rate_zone": rate_zone,
    }
