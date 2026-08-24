from __future__ import annotations

import pandas as pd
import pytest

from src.product_matcher import coating_type, product_form, rank_product_matches


@pytest.fixture
def catalog() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "item_code": "POLY-LID-CLOSE",
                "description": "Printed lid",
                "product_group": "Glued Lid 4PT Poly",
                "length_mm": 590,
                "width_mm": 390,
                "height_mm": 150,
                "board_gsm": 1000,
                "bom_available": 1,
            },
            {
                "item_code": "POLY-LID-FAR",
                "description": "Printed lid",
                "product_group": "Glued Lid 4PT Poly",
                "length_mm": 500,
                "width_mm": 300,
                "height_mm": 100,
                "board_gsm": 800,
                "bom_available": 1,
            },
            {
                "item_code": "NO-POLY-LID",
                "description": "Unprinted lid no poly",
                "product_group": "Glued Lid 4PT No Pol",
                "length_mm": 590,
                "width_mm": 390,
                "height_mm": 150,
                "board_gsm": 1000,
                "bom_available": 1,
            },
            {
                "item_code": "POLY-BASE",
                "description": "Printed base",
                "product_group": "Glued Base 4PT Poly",
                "length_mm": 590,
                "width_mm": 390,
                "height_mm": 150,
                "board_gsm": 1000,
                "bom_available": 1,
            },
            {
                "item_code": "NO-BOM",
                "description": "Printed lid",
                "product_group": "Glued Lid 4PT Poly",
                "length_mm": 590,
                "width_mm": 390,
                "height_mm": 150,
                "board_gsm": 1000,
                "bom_available": 0,
            },
        ]
    )


def test_product_labels_handle_poly_and_no_poly() -> None:
    assert product_form("Glued Lid 4PT Poly") == "Lid"
    assert product_form("Glud Base 4PT No Pol") == "Base"
    assert coating_type("Glued Lid 4PT Poly") == "Poly coated"
    assert coating_type("Glued Lid 4PT No Pol") == "No poly"


def test_matcher_filters_then_ranks_closest_usable_product(
    catalog: pd.DataFrame,
) -> None:
    matches = rank_product_matches(
        catalog,
        requested_form="Lid",
        requested_coating="Poly coated",
        length_mm=585,
        width_mm=390,
        height_mm=150,
        gsm=1000,
    )

    assert matches["item_code"].tolist() == ["POLY-LID-CLOSE", "POLY-LID-FAR"]
    assert "NO-BOM" not in matches["item_code"].tolist()
    assert matches.iloc[0]["difference_length_mm"] == pytest.approx(5)
    assert matches.iloc[0]["difference_board_gsm"] == pytest.approx(0)


def test_matcher_accepts_a_partial_target_specification(catalog: pd.DataFrame) -> None:
    matches = rank_product_matches(
        catalog,
        requested_form="Any product",
        requested_coating="Any coating",
        length_mm=0,
        width_mm=390,
        height_mm=150,
        gsm=1000,
    )

    assert not matches.empty
    assert matches.iloc[0]["difference_width_mm"] == pytest.approx(0)
    assert pd.isna(matches.iloc[0]["difference_length_mm"])
