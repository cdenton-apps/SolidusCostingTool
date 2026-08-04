from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


BOOKING_SURCHARGES = {"Standard": 0.0, "AM/PM": 7.0, "Timed": 19.0}
MCDOWELLS_FULL_LOAD_SURCHARGE = 40.0
MAX_PALLETS_PER_LOAD = 26


class TransportLookupError(ValueError):
    pass


@dataclass(frozen=True)
class TransportQuote:
    rate_zone: str
    service: str
    vendor: str
    pallet_count: int
    load_count: int
    base_cost: float
    booking_surcharge: float
    full_load_surcharge: float
    total_cost: float

    def to_dict(self) -> dict[str, str | float | int]:
        return asdict(self)


def _normalised_zone(value: str) -> str:
    return re.sub(r"[^A-Z0-9+]", "", value.upper())


def _parse_postcode(postcode: str) -> tuple[str, int | None]:
    compact = re.sub(r"\s+", "", str(postcode).upper())
    match = re.match(r"^([A-Z]{1,2})(\d{1,2})", compact)
    if not match:
        raise TransportLookupError("Enter a valid UK postcode, such as BD20 0AA.")
    return match.group(1), int(match.group(2))


def match_rate_zone(postcode: str, available_zones: list[str]) -> str:
    area, district = _parse_postcode(postcode)
    by_normalised = {_normalised_zone(zone): zone for zone in available_zones}

    def find(label: str) -> str:
        zone = by_normalised.get(_normalised_zone(label))
        if not zone:
            raise TransportLookupError(
                f"No haulier rate zone is configured for {postcode}."
            )
        return zone

    if district is None:
        return find(area)

    if area == "DN":
        if district <= 14:
            return find("DN 1-14")
        if district <= 25:
            return find("DN 15-25")
        if district <= 40:
            return find("DN 26-40")
        return find(area)
    if area == "LL":
        if district <= 34:
            return find("LL1-34")
        if district <= 78:
            return find("LL 35-78")
        return find(area)
    if area == "LA":
        return find("LA 1-10" if district <= 10 else "LA 11+")
    if area == "PE":
        if district <= 20:
            return find("PE 1-20")
        if district <= 29:
            return find("PE 21-29")
        if district == 30:
            return find("PE 30")
        return find(area)
    if area == "YO":
        return find("YO1-8,19,51,60" if district in {*range(1, 9), 19, 51, 60} else "YO (All Other)")
    if area == "SY":
        return find("SY 1-2" if district in {1, 2} else "SY(All Others)")
    if area == "PO":
        if district <= 29:
            return find("PO1-29")
        if district <= 41:
            return find("PO 30-41")
        return find(area)
    if area == "TN":
        special = {4, 5, 6, 7, 16, 23, 24, 27}
        return find("TN 4-7, 16,23,24,27" if district in special else "TN (All others)")
    if area == "KA":
        if district in {27, 28}:
            return find("KA27 - 28")
        if district <= 26 or district == 29:
            return find("KA1-26 & 29")
        return find(area)
    if area == "KY":
        return find("KY 11 - 12" if district in {11, 12} else "KY 1 - 10 & 13+")
    if area == "PA":
        return find("PA1-19" if district <= 19 else "PA 20+")
    if area == "DG":
        if district in {8, 9}:
            return find("DG 8-9")
        if district <= 16:
            return find("DG 1-7 & 10-16")
        return find("DG")
    if area == "PH":
        if district <= 7 or district == 14:
            return find("PH 1-7, 14")
        if district <= 13:
            return find("PH 8-13")
        return find("PH 15+")
    return find(area)


class HaulierRateTable:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.rates = pd.read_csv(self.path)
        self.rates = self.rates.drop_duplicates(
            subset=["zone", "service", "vendor"], keep="first"
        )

    @property
    def available_zones(self) -> list[str]:
        return sorted(self.rates["zone"].dropna().astype(str).unique().tolist())

    def quote_options(
        self,
        *,
        postcode: str,
        pallet_count: int,
        service: str,
        booking: str = "Standard",
    ) -> list[TransportQuote]:
        if pallet_count <= 0:
            raise TransportLookupError("Pallet count must be greater than zero.")
        if booking not in BOOKING_SURCHARGES:
            raise TransportLookupError(f"Unknown booking type: {booking}.")

        zone = match_rate_zone(postcode, self.available_zones)
        matches = self.rates[
            (self.rates["zone"] == zone) & (self.rates["service"] == service)
        ]
        if matches.empty:
            raise TransportLookupError(
                f"No {service.lower()} rates are configured for {zone}."
            )

        full_loads, remainder = divmod(int(pallet_count), MAX_PALLETS_PER_LOAD)
        load_sizes = [MAX_PALLETS_PER_LOAD] * full_loads
        if remainder:
            load_sizes.append(remainder)

        quotes: list[TransportQuote] = []
        for _, row in matches.iterrows():
            prices = [row.get(f"pallet_{load_size}") for load_size in load_sizes]
            if any(pd.isna(price) for price in prices):
                continue
            base_cost = sum(float(price) for price in prices)
            booking_total = BOOKING_SURCHARGES[booking] * len(load_sizes)
            full_load_surcharge = (
                MCDOWELLS_FULL_LOAD_SURCHARGE * full_loads
                if str(row["vendor"]).lower() == "mcdowells"
                else 0.0
            )
            total = base_cost + booking_total + full_load_surcharge
            quotes.append(
                TransportQuote(
                    rate_zone=zone,
                    service=service,
                    vendor=str(row["vendor"]),
                    pallet_count=int(pallet_count),
                    load_count=len(load_sizes),
                    base_cost=round(base_cost, 2),
                    booking_surcharge=round(booking_total, 2),
                    full_load_surcharge=round(full_load_surcharge, 2),
                    total_cost=round(total, 2),
                )
            )
        if not quotes:
            raise TransportLookupError(
                f"Neither haulier has a complete {pallet_count}-pallet rate for {zone}."
            )
        return sorted(quotes, key=lambda quote: quote.total_cost)
