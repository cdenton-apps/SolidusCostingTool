from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.repository import CsvRepository


ARTICLE_PATTERN = re.compile(r"(?:SHT\d+(?:/[A-Z])?|[234]-\d+)", re.IGNORECASE)


def clean(value: Any) -> Any:
    return None if pd.isna(value) else value


def number(value: Any) -> float | None:
    if pd.isna(value):
        return None
    try:
        parsed = float(
            str(value)
            .lower()
            .replace("gsm", "")
            .replace("g", "")
            .replace(",", "")
            .strip()
        )
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def article_aliases(value: Any) -> list[str]:
    if pd.isna(value):
        return []
    aliases: list[str] = []
    for match in ARTICLE_PATTERN.finditer(str(value).upper()):
        alias = match.group(0).strip()
        for candidate in [alias, alias.removesuffix("/A")]:
            if candidate and candidate not in aliases:
                aliases.append(candidate)
    return aliases


def analysis_values(row: pd.Series) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index in range(1, 21):
        name = row.get(f"AnalysisName\\{index}")
        if pd.notna(name):
            result[str(name).strip()] = clean(row.get(f"AnalysisValue\\{index}"))
    return result


def preferred_dimension(values: dict[str, Any], board_key: str, item_key: str) -> float | None:
    preferred = number(values.get(board_key))
    return preferred if preferred and preferred > 0 else number(values.get(item_key))


def prepare_prices(path: Path) -> pd.DataFrame:
    source = pd.read_excel(path, sheet_name="IC Pricelist Rolling ", dtype=object)
    prices = pd.DataFrame(
        {
            "article_no": source["Article No."].astype(str).str.strip(),
            "sales_article_code": source["Sales article code"],
            "grade_article": source["Grade Article"],
            "width_mm": pd.to_numeric(source["Width"], errors="coerce"),
            "length_mm": pd.to_numeric(source["Length"], errors="coerce"),
            "unit_no": pd.to_numeric(source["Unit no."], errors="coerce"),
            "price_per_tonne": pd.to_numeric(
                source["Apr-26\nPrice/1000 KG"], errors="coerce"
            ),
        }
    )
    prices["board_gsm"] = pd.to_numeric(
        prices["sales_article_code"].astype(str).str.extract(r"/([0-9]{4})/")[0],
        errors="coerce",
    )
    prices["article_aliases"] = prices["article_no"].map(
        lambda value: "|".join(article_aliases(value))
    )
    prices["price_period"] = "Apr-26"
    prices = prices[prices["article_no"].ne("nan")].reset_index(drop=True)
    return prices


def resolve_price(board: dict[str, Any], prices: pd.DataFrame) -> dict[str, Any]:
    candidates: list[str] = []
    for field in ["board_code_raw", "legacy_code"]:
        for alias in article_aliases(board.get(field)):
            if alias not in candidates:
                candidates.append(alias)

    positive = prices[pd.to_numeric(prices["price_per_tonne"], errors="coerce") > 0]
    matched = positive.iloc[0:0]
    if candidates:
        candidate_set = set(candidates)
        matched = positive[
            positive["article_aliases"].fillna("").map(
                lambda value: bool(candidate_set.intersection(str(value).split("|")))
            )
        ]

    board_gsm = number(board.get("board_gsm"))
    width = number(board.get("board_width_mm"))
    length = number(board.get("board_length_mm"))

    def dimension_matches(frame: pd.DataFrame) -> pd.DataFrame:
        if not board_gsm or not width or not length:
            return frame.iloc[0:0]
        return frame[
            (pd.to_numeric(frame["board_gsm"], errors="coerce") == board_gsm)
            & (
                (
                    (pd.to_numeric(frame["width_mm"], errors="coerce") == width)
                    & (pd.to_numeric(frame["length_mm"], errors="coerce") == length)
                )
                | (
                    (pd.to_numeric(frame["width_mm"], errors="coerce") == length)
                    & (pd.to_numeric(frame["length_mm"], errors="coerce") == width)
                )
            )
        ]

    source = ""
    chosen = matched.iloc[0:0]
    if not matched.empty:
        dimensional = dimension_matches(matched)
        chosen = dimensional if not dimensional.empty else matched
        if chosen["price_per_tonne"].nunique(dropna=True) == 1:
            source = "Mill price: article code"
        else:
            chosen = chosen.iloc[0:0]

    if chosen.empty:
        dimensional = dimension_matches(positive)
        if (
            not dimensional.empty
            and dimensional["price_per_tonne"].nunique(dropna=True) == 1
        ):
            chosen = dimensional
            source = "Mill price: unique size/GSM"

    if chosen.empty:
        return {
            "resolved_article_no": "",
            "price_per_tonne": None,
            "price_period": "",
            "price_source": "No unambiguous Apr-26 mill match",
            "resolved_width_mm": None,
            "resolved_length_mm": None,
            "resolved_gsm": None,
        }

    row = chosen.iloc[0]
    return {
        "resolved_article_no": row["article_no"],
        "price_per_tonne": float(row["price_per_tonne"]),
        "price_period": row["price_period"],
        "price_source": source,
        "resolved_width_mm": number(row["width_mm"]),
        "resolved_length_mm": number(row["length_mm"]),
        "resolved_gsm": number(row["board_gsm"]),
    }


def prepare_stock(
    path: Path,
    bom_codes: set[str],
    prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if path.suffix.lower() == ".csv":
        source = pd.read_csv(path, dtype=object, keep_default_na=False)
    else:
        source = pd.read_excel(path, sheet_name="Stock Item Info", dtype=object)
    finished: list[dict[str, Any]] = []
    boards: list[dict[str, Any]] = []

    for _, row in source.iterrows():
        code = str(row["Stock item code"]).strip()
        analysis = analysis_values(row)
        common = {
            "item_code": code,
            "item_name": clean(row["Stock item name"]),
            "description": clean(row["Stock item description"]),
            "product_group": clean(row["Product group"]),
            "manufacturing_site": clean(row["Manufacturer's name"]),
            "net_mass_kg": number(row["Net mass"]),
            "allow_sales_order": clean(row["Allow Sales order"]),
            "legacy_code": clean(analysis.get("Legacy Code")),
            "double_stack": clean(analysis.get("Doublestack")),
            "pallet_size": clean(analysis.get("Pallet Size")),
            "mrp_type": clean(analysis.get("MRP Type")),
            "length_mm": number(analysis.get("Length")),
            "width_mm": number(analysis.get("Width")),
            "height_mm": number(analysis.get("Height")),
            "board_gsm": number(analysis.get("Grade / Gram")),
            "board_width_mm": number(analysis.get("Boardwidth/Reel Width")),
            "board_length_mm": number(analysis.get("Boardlength/Chop")),
            "bundle_quantity": number(analysis.get("BundleQty / Reel Core ID")),
            "bundles_per_layer": number(
                analysis.get("Bundles Per Layer / Bundle Type")
            ),
            "layers_per_pallet": number(analysis.get("Layers Per Pallet")),
            "pallet_height_mm": number(analysis.get("Pallet Height")),
            "product_state": clean(analysis.get("Product State")),
            "number_of_colours": number(analysis.get("Number Of Colours")),
            "fsc": clean(analysis.get("FSC")),
            "pallet_quantity": number(analysis.get("Pallet Qty")),
            "board_code": clean(analysis.get("Board Code")),
            "market_segment": clean(analysis.get("Market Segment")),
        }
        if code.upper().startswith("BOX"):
            finished.append({**common, "bom_available": int(code in bom_codes)})
        elif code.upper().startswith("BRD"):
            board = {
                "board_item_code": code,
                "board_item_name": common["item_name"],
                "product_group": common["product_group"],
                "manufacturing_site": common["manufacturing_site"],
                "product_state": common["product_state"],
                "legacy_code": common["legacy_code"],
                "board_code_raw": common["board_code"],
                "board_gsm": common["board_gsm"],
                "board_width_mm": preferred_dimension(
                    analysis, "Boardwidth/Reel Width", "Width"
                ),
                "board_length_mm": preferred_dimension(
                    analysis, "Boardlength/Chop", "Length"
                ),
                "fsc": common["fsc"],
            }
            boards.append({**board, **resolve_price(board, prices)})

    return pd.DataFrame(finished), pd.DataFrame(boards)


def prepare_bom(path: Path) -> pd.DataFrame:
    workbook = pd.ExcelFile(path)
    sheet_name = "BOM Info" if "BOM Info" in workbook.sheet_names else workbook.sheet_names[0]
    source = pd.read_excel(workbook, sheet_name=sheet_name, dtype=object)
    source.columns = [
        re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
        for column in source.columns
    ]
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert supplied costing workbooks to app CSV feeds.")
    parser.add_argument("--costing-workbook", required=True, type=Path)
    parser.add_argument(
        "--bom-workbook",
        type=Path,
        help="Optional full BOM export workbook. The first sheet is used when there is no BOM Info sheet.",
    )
    parser.add_argument(
        "--stock-csv",
        type=Path,
        help="Optional newer Sage stock export/import CSV to use instead of the workbook stock sheet.",
    )
    parser.add_argument("--board-prices", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    prices = prepare_prices(args.board_prices)
    bom = prepare_bom(args.bom_workbook or args.costing_workbook)
    finished, boards = prepare_stock(
        args.stock_csv or args.costing_workbook,
        set(bom["bomcode"].astype(str)),
        prices,
    )
    bom = bom.rename(
        columns={
            "bomcode": "bom_code",
            "bomdescription": "bom_description",
            "costtype": "cost_type",
            "processgroup": "process_group",
            "machinebucket": "machine_bucket",
            "sourcelinetype": "source_line_type",
            "linesequence": "line_sequence",
            "operationreference": "operation_reference",
            "operationdescription": "operation_description",
            "costcode": "cost_code",
            "costdescription": "cost_description",
            "unitofmeasure": "unit_of_measure",
            "runhours": "run_hours",
            "systemquantityperrun": "system_quantity_per_run",
            "overridespeed": "override_speed",
            "effectivequantityperrun": "effective_quantity_per_run",
            "supplierlistprice": "supplier_list_price",
            "standardcost": "standard_cost",
            "fallbacklastprice": "fallback_last_price",
            "costrate": "cost_rate",
            "unitcost": "unit_cost",
            "extendedcost": "extended_cost",
            "isinformationalrow": "is_informational_row",
        }
    )

    finished.to_csv(args.output_dir / "current_items.csv", index=False)
    boards.to_csv(args.output_dir / "board_items.csv", index=False)
    prices.to_csv(args.output_dir / "board_prices.csv", index=False)
    bom.to_csv(
        args.output_dir / "bom_costs.csv.gz",
        index=False,
        compression="gzip",
    )
    material_summaries = CsvRepository(args.output_dir).rebuild_material_summary()

    priced = int(pd.to_numeric(boards["price_per_tonne"], errors="coerce").notna().sum())
    print(
        f"Wrote {len(finished)} finished items, {len(boards)} board items "
        f"({priced} with an unambiguous Apr-26 rate), {len(prices)} price rows "
        f"and {len(bom)} BOM lines. Built {len(material_summaries)} material summaries."
    )


if __name__ == "__main__":
    main()
