from __future__ import annotations

import math
from typing import Any

import pandas as pd


PRODUCT_FORMS = ["Any product", "Lid", "Base", "One piece / other"]
COATING_OPTIONS = ["Any coating", "Poly coated", "No poly"]


def product_form(product_group: Any, description: Any = "") -> str:
    text = f"{product_group or ''} {description or ''}".casefold()
    if "lid" in text:
        return "Lid"
    if "base" in text or "bse" in text:
        return "Base"
    return "One piece / other"


def coating_type(product_group: Any, description: Any = "") -> str:
    text = f"{product_group or ''} {description or ''}".casefold()
    if "no pol" in text or "no poly" in text:
        return "No poly"
    if "poly" in text or " pol" in text:
        return "Poly coated"
    return "Not stated"


def rank_product_matches(
    catalog: pd.DataFrame,
    *,
    requested_form: str,
    requested_coating: str,
    length_mm: float,
    width_mm: float,
    height_mm: float,
    gsm: float,
    limit: int = 8,
) -> pd.DataFrame:
    """Return the closest usable products after applying form/coating filters.

    Every search field is optional. Supplied dimensions and GSM are used for
    ranking; omitted values are ignored. The unrounded numeric differences are
    retained so the selection screen can show users exactly how each suggestion
    differs from the requested specification.
    """
    raw_targets = {
        "length_mm": float(length_mm),
        "width_mm": float(width_mm),
        "height_mm": float(height_mm),
        "board_gsm": float(gsm),
    }
    if any(not math.isfinite(value) or value < 0 for value in raw_targets.values()):
        raise ValueError("Search measurements must be positive numbers.")
    targets = {
        column: value for column, value in raw_targets.items() if value > 0
    }
    if catalog.empty:
        return catalog.copy()

    work = catalog.copy()
    for column in raw_targets:
        work[column] = pd.to_numeric(work.get(column), errors="coerce")
    if targets:
        work = work.dropna(subset=list(targets)).copy()
        work = work.loc[(work[list(targets)] > 0).all(axis=1)].copy()
    if "bom_available" in work:
        work = work.loc[
            pd.to_numeric(work["bom_available"], errors="coerce").fillna(0).gt(0)
        ].copy()

    work["suggested_form"] = [
        product_form(group, description)
        for group, description in zip(
            work.get("product_group", ""), work.get("description", "")
        )
    ]
    work["suggested_coating"] = [
        coating_type(group, description)
        for group, description in zip(
            work.get("product_group", ""), work.get("description", "")
        )
    ]
    if requested_form != "Any product":
        work = work.loc[work["suggested_form"].eq(requested_form)].copy()
    if requested_coating != "Any coating":
        work = work.loc[work["suggested_coating"].eq(requested_coating)].copy()
    if work.empty:
        return work

    dimension_terms: list[pd.Series] = []
    length_target = targets.get("length_mm")
    width_target = targets.get("width_mm")
    work["difference_length_mm"] = math.nan
    work["difference_width_mm"] = math.nan
    if length_target and width_target:
        target_long = max(length_target, width_target)
        target_short = min(length_target, width_target)
        product_long = work[["length_mm", "width_mm"]].max(axis=1)
        product_short = work[["length_mm", "width_mm"]].min(axis=1)
        dimension_terms.extend(
            [
                ((product_long - target_long) / target_long).pow(2),
                ((product_short - target_short) / target_short).pow(2),
            ]
        )
        work["difference_length_mm"] = product_long - target_long
        work["difference_width_mm"] = product_short - target_short
    elif length_target or width_target:
        side_target = float(length_target or width_target)
        length_difference = work["length_mm"] - side_target
        width_difference = work["width_mm"] - side_target
        use_length = length_difference.abs().le(width_difference.abs())
        nearest_side = work["length_mm"].where(use_length, work["width_mm"])
        dimension_terms.append(((nearest_side - side_target) / side_target).pow(2))
        difference_column = (
            "difference_length_mm" if length_target else "difference_width_mm"
        )
        work[difference_column] = nearest_side - side_target
    if "height_mm" in targets:
        dimension_terms.append(
            ((work["height_mm"] - targets["height_mm"]) / targets["height_mm"]).pow(2)
        )
    work["difference_height_mm"] = (
        work["height_mm"] - raw_targets["height_mm"]
        if raw_targets["height_mm"] > 0
        else math.nan
    )

    distance_parts: list[tuple[pd.Series, float]] = []
    if dimension_terms:
        dimension_distance = sum(dimension_terms) / len(dimension_terms)
        distance_parts.append(
            (dimension_distance, 0.8 if "board_gsm" in targets else 1.0)
        )
    if "board_gsm" in targets:
        gsm_distance = (
            (work["board_gsm"] - targets["board_gsm"]) / targets["board_gsm"]
        ).pow(2)
        distance_parts.append((gsm_distance, 0.2 if dimension_terms else 1.0))
    if distance_parts:
        work["match_distance"] = sum(
            distance * weight for distance, weight in distance_parts
        ).pow(0.5)
    else:
        work["match_distance"] = 0.0
    work["difference_board_gsm"] = (
        work["board_gsm"] - raw_targets["board_gsm"]
        if raw_targets["board_gsm"] > 0
        else math.nan
    )

    return (
        work.sort_values(["match_distance", "item_code"], kind="stable")
        .head(max(1, int(limit)))
        .reset_index(drop=True)
    )
