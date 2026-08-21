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

    Dimensions and GSM are used only for ranking. The unrounded numeric
    differences are retained so the selection screen can show users exactly
    how each suggestion differs from their requested specification.
    """
    targets = {
        "length_mm": float(length_mm),
        "width_mm": float(width_mm),
        "height_mm": float(height_mm),
        "board_gsm": float(gsm),
    }
    if any(not math.isfinite(value) or value <= 0 for value in targets.values()):
        raise ValueError("Enter the three finished dimensions and GSM to find matches.")
    if catalog.empty:
        return catalog.copy()

    work = catalog.copy()
    for column in targets:
        work[column] = pd.to_numeric(work.get(column), errors="coerce")
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

    dimension_columns = ["length_mm", "width_mm", "height_mm"]
    dimension_distance = sum(
        ((work[column] - targets[column]) / targets[column]).pow(2)
        for column in dimension_columns
    ) / len(dimension_columns)
    gsm_distance = (
        (work["board_gsm"] - targets["board_gsm"]) / targets["board_gsm"]
    ).pow(2)
    work["match_distance"] = (dimension_distance * 0.8 + gsm_distance * 0.2).pow(0.5)
    for column, target in targets.items():
        work[f"difference_{column}"] = work[column] - target

    return (
        work.sort_values(["match_distance", "item_code"], kind="stable")
        .head(max(1, int(limit)))
        .reset_index(drop=True)
    )
