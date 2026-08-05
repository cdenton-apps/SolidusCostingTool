from __future__ import annotations

import os
import math
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from filelock import FileLock


SPECIFICATION_COLUMNS = [
    "customer_name",
    "item_code",
    "item_name",
    "description",
    "material",
    "product_group",
    "manufacturing_site",
    "net_mass_kg",
    "legacy_code",
    "double_stack",
    "pallet_size",
    "mrp_type",
    "length_mm",
    "width_mm",
    "height_mm",
    "board_gsm",
    "board_width_mm",
    "board_length_mm",
    "bundle_quantity",
    "bundles_per_layer",
    "layers_per_pallet",
    "pallet_height_mm",
    "product_state",
    "number_of_colours",
    "fsc",
    "pallet_quantity",
    "board_code",
    "market_segment",
    "fulfilment_type",
    "quantity_input_mode",
    "order_quantity",
    "order_pallets",
    "agreement_term_months",
    "delivery_pallets_per_calloff",
    "estimated_delivery_count",
    "pallet_holding_charge_per_pallet_per_week",
    "delivery_postcode",
    "delivery_method",
    "transport_service",
    "transport_vendor_preference",
    "transport_vendor",
    "transport_booking",
    "transport_rate_zone",
    "transport_manual_override",
]

COST_INPUT_COLUMNS = [
    "bom_available",
    "materials_cost_per_1000",
    "board_item_code",
    "board_article_code",
    "board_price_per_tonne",
    "board_price_period",
    "board_price_source",
    "board_tonnes_per_1000",
    "board_cost_per_1000",
    "other_components_cost_per_1000",
    "component_template_item_code",
    "units_out",
    "material_cost_source",
    "manual_adjustment_per_1000",
    "machine_hours_per_1000",
    "machine_time_source",
]

NUMERIC_COST_INPUT_COLUMNS = [
    "bom_available",
    "materials_cost_per_1000",
    "board_price_per_tonne",
    "board_tonnes_per_1000",
    "board_cost_per_1000",
    "other_components_cost_per_1000",
    "units_out",
    "manual_adjustment_per_1000",
    "machine_hours_per_1000",
]

CALCULATION_COLUMNS = [
    "net_weight_kg_per_1000",
    "pallet_count",
    "transport_total",
    "material_base_per_1000",
    "transport_cost_per_1000",
    "pricing_base_per_1000",
    "pricing_base_per_item",
    "spread_percent",
    "spread_value_per_1000",
    "selling_price_per_1000",
    "selling_price_per_item",
    "total_machine_hours",
    "total_spread_value",
    "spread_per_machine_hour",
]

HISTORY_COLUMNS = [
    "costing_id",
    "revision",
    "source_item_code",
    "created_at_utc",
    "created_by",
    "created_by_name",
    "quote_reference",
    "customer_contact",
    "notes",
    *SPECIFICATION_COLUMNS,
    *COST_INPUT_COLUMNS,
    *CALCULATION_COLUMNS,
]

BOM_TOTAL_RENAMES = {
    "bom_materials": "imported_bom_materials_per_1000",
    "bom_print_machine": "print_machine_cost_per_1000",
    "bom_die_cut_machine": "die_cut_machine_cost_per_1000",
    "bom_fold_glue_machine": "fold_glue_machine_cost_per_1000",
    "bom_other_machine": "other_machine_cost_per_1000",
    "bom_labour": "labour_cost_per_1000",
    "bom_machine_total": "imported_machine_cost_per_1000",
    "bom_total_unit_cost": "imported_bom_total_per_1000",
}


class CsvRepository:
    """CSV-backed repository with imported item/BOM feeds and append-only history."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.items_path = self.data_dir / "current_items.csv"
        self.bom_path = self.data_dir / "bom_costs.csv"
        self.board_items_path = self.data_dir / "board_items.csv"
        self.board_prices_path = self.data_dir / "board_prices.csv"
        self.haulier_path = self.data_dir / "haulier_rates.csv"
        self.history_path = self.data_dir / "saved_costings.csv"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load_bom_lines(self, item_code: str | None = None) -> pd.DataFrame:
        if not self.bom_path.exists():
            return pd.DataFrame()
        bom = pd.read_csv(self.bom_path)
        if item_code is not None and "bom_code" in bom:
            bom = bom[bom["bom_code"] == item_code]
        return bom

    def load_bom_summary(self) -> pd.DataFrame:
        bom = self.load_bom_lines()
        if bom.empty:
            return pd.DataFrame(columns=["item_code", *BOM_TOTAL_RENAMES.values()])
        available = [column for column in BOM_TOTAL_RENAMES if column in bom]
        summary = (
            bom.sort_values(["bom_code", "line_sequence"])
            .groupby("bom_code", as_index=False)
            .first()[["bom_code", *available]]
            .rename(columns={"bom_code": "item_code", **BOM_TOTAL_RENAMES})
        )
        return summary

    def load_board_items(self) -> pd.DataFrame:
        if not self.board_items_path.exists():
            return pd.DataFrame()
        return pd.read_csv(self.board_items_path)

    def load_board_prices(self) -> pd.DataFrame:
        if not self.board_prices_path.exists():
            return pd.DataFrame()
        return pd.read_csv(self.board_prices_path)

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @classmethod
    def _machine_time_from_frames(
        cls,
        item_code: str,
        bom: pd.DataFrame,
    ) -> dict[str, Any]:
        """Calculate machine hours per 1,000 from BOM operation speeds.

        Direct operations provide run hours and effective output per run. Rolled
        child machine rows do not carry the child speed, so their already-scaled
        machine value is divided by the matching imported machine rate only to
        recover time. No machine value is added to the commercial pricing base.
        """
        if bom.empty or "bom_code" not in bom or "cost_type" not in bom:
            return {
                "machine_hours_per_1000": 0.0,
                "machine_time_source": "No BOM machine-time profile",
            }
        item_lines = bom[bom["bom_code"].astype(str) == str(item_code)].copy()
        if item_lines.empty:
            return {
                "machine_hours_per_1000": 0.0,
                "machine_time_source": "No BOM machine-time profile",
            }

        informational = pd.to_numeric(
            item_lines.get("is_informational_row", 0), errors="coerce"
        ).fillna(0)
        machine_lines = item_lines[
            item_lines["cost_type"].astype(str).eq("Machine")
            & informational.eq(0)
        ].copy()
        run_hours = pd.to_numeric(
            machine_lines.get("run_hours"), errors="coerce"
        )
        effective_quantity = pd.to_numeric(
            machine_lines.get("effective_quantity_per_run"), errors="coerce"
        ).fillna(
            pd.to_numeric(
                machine_lines.get("system_quantity_per_run"), errors="coerce"
            )
        )
        valid_direct = run_hours.gt(0) & effective_quantity.gt(0)
        direct_hours = float(
            (run_hours[valid_direct] / effective_quantity[valid_direct]).sum()
        )

        all_machine_lines = bom[bom["cost_type"].astype(str).eq("Machine")].copy()
        all_machine_lines["numeric_rate"] = pd.to_numeric(
            all_machine_lines.get("cost_rate"), errors="coerce"
        )
        valid_rates = all_machine_lines[all_machine_lines["numeric_rate"].gt(0)]
        rate_by_bucket = (
            valid_rates.assign(
                bucket=valid_rates.get("machine_bucket", "").astype(str)
            )
            .groupby("bucket")["numeric_rate"]
            .median()
            .to_dict()
        )
        overall_rate = (
            float(valid_rates["numeric_rate"].median())
            if not valid_rates.empty
            else 0.0
        )
        rolled_machine = item_lines[
            item_lines["cost_type"].astype(str).eq("Rolled Child")
            & item_lines.get("cost_description", "")
            .astype(str)
            .str.contains("Machine", case=False, regex=False)
        ]
        rolled_hours = 0.0
        for _, line in rolled_machine.iterrows():
            description = str(line.get("cost_description", "")).lower()
            if "print" in description:
                bucket = "Print"
            elif "die" in description:
                bucket = "Die Cut"
            elif "fold" in description or "glue" in description:
                bucket = "Fold Glue"
            else:
                bucket = ""
            rate = float(rate_by_bucket.get(bucket, overall_rate) or 0)
            extended_value = pd.to_numeric(
                line.get("extended_cost"), errors="coerce"
            )
            extended = float(extended_value) if pd.notna(extended_value) else 0.0
            if rate > 0 and extended > 0:
                rolled_hours += extended / rate

        total_hours = direct_hours + rolled_hours
        if total_hours <= 0:
            source = "No BOM machine-time profile"
        elif rolled_hours > 0:
            source = "BOM operation speeds plus rolled-child machine time"
        else:
            source = "BOM operation speeds"
        return {
            "machine_hours_per_1000": round(total_hours, 6),
            "machine_time_source": source,
        }

    @classmethod
    def _board_tonnes_per_1000(
        cls, line: pd.Series, board: pd.Series | None
    ) -> float | None:
        quantity = cls._positive_number(line.get("quantity"))
        if quantity is None:
            return None
        if "tonne" in str(line.get("unit_of_measure", "")).lower():
            return quantity
        if board is None:
            return None
        width = cls._positive_number(board.get("board_width_mm")) or cls._positive_number(
            board.get("resolved_width_mm")
        )
        length = cls._positive_number(
            board.get("board_length_mm")
        ) or cls._positive_number(board.get("resolved_length_mm"))
        gsm = cls._positive_number(board.get("board_gsm")) or cls._positive_number(
            board.get("resolved_gsm")
        )
        if not width or not length or not gsm:
            return None
        return quantity * width * length * gsm / 1_000_000_000

    @classmethod
    def _material_breakdown_from_frames(
        cls,
        item_code: str,
        bom: pd.DataFrame,
        boards: pd.DataFrame,
    ) -> dict[str, Any]:
        item_lines = bom[bom["bom_code"].astype(str) == str(item_code)].copy()
        machine_time = cls._machine_time_from_frames(item_code, bom)
        if item_lines.empty:
            return {
                "summary": {
                    "materials_cost_per_1000": 0.0,
                    "board_item_code": "",
                    "board_article_code": "",
                    "board_price_per_tonne": 0.0,
                    "board_price_period": "",
                    "board_price_source": "No BOM board component",
                    "board_tonnes_per_1000": 0.0,
                    "board_cost_per_1000": 0.0,
                    "other_components_cost_per_1000": 0.0,
                    "material_cost_source": "No BOM material lines",
                    **machine_time,
                },
                "lines": pd.DataFrame(),
            }

        informational = pd.to_numeric(
            item_lines.get("is_informational_row", 0), errors="coerce"
        ).fillna(0)
        materials = item_lines[
            item_lines["cost_type"].astype(str).eq("Material")
            & informational.eq(0)
        ].copy()
        board_lookup = (
            boards.set_index("board_item_code", drop=False)
            if not boards.empty and "board_item_code" in boards
            else pd.DataFrame()
        )

        detail_rows: list[dict[str, Any]] = []
        board_total = 0.0
        board_tonnes = 0.0
        other_total = 0.0
        board_items: list[str] = []
        articles: list[str] = []
        sources: list[str] = []
        periods: list[str] = []

        for _, line in materials.iterrows():
            component_code = str(line.get("cost_code", ""))
            extended_value = pd.to_numeric(line.get("extended_cost"), errors="coerce")
            extended = float(extended_value) if pd.notna(extended_value) else 0.0
            if not component_code.upper().startswith("BRD"):
                other_total += extended
                detail_rows.append(
                    {
                        "component_type": "Other component",
                        "component_code": component_code,
                        "description": line.get("cost_description", ""),
                        "quantity": line.get("quantity", 0),
                        "unit_of_measure": line.get("unit_of_measure", ""),
                        "rate": line.get("unit_cost", 0),
                        "cost_per_1000": extended,
                        "source": "BOM component rate",
                    }
                )
                continue

            board = None
            if not board_lookup.empty and component_code in board_lookup.index:
                board = board_lookup.loc[component_code]
                if isinstance(board, pd.DataFrame):
                    board = board.iloc[0]
            tonnes = cls._board_tonnes_per_1000(line, board)
            rate = cls._positive_number(board.get("price_per_tonne")) if board is not None else None
            source = str(board.get("price_source", "")) if board is not None else ""
            article = str(board.get("resolved_article_no", "")) if board is not None else ""
            period = str(board.get("price_period", "")) if board is not None else ""

            if rate is not None and tonnes is not None:
                cost = rate * tonnes
            else:
                rolled = item_lines[
                    item_lines["cost_type"].astype(str).eq("Rolled Child")
                    & item_lines["cost_code"].astype(str).eq(component_code)
                    & item_lines["cost_description"]
                    .astype(str)
                    .str.contains("Labour|Machine", case=False, regex=True)
                ]
                excluded_conversion = pd.to_numeric(
                    rolled.get("extended_cost", 0), errors="coerce"
                ).fillna(0).sum()
                cost = max(0.0, extended - float(excluded_conversion))
                rate = cost / tonnes if tonnes else None
                source = "BOM material fallback (machine/labour removed)"
                article = ""
                period = ""

            board_total += cost
            if tonnes:
                board_tonnes += tonnes
            if component_code not in board_items:
                board_items.append(component_code)
            if article and article != "nan" and article not in articles:
                articles.append(article)
            if source and source not in sources:
                sources.append(source)
            if period and period != "nan" and period not in periods:
                periods.append(period)
            detail_rows.append(
                {
                    "component_type": "Board",
                    "component_code": component_code,
                    "description": line.get("cost_description", ""),
                    "quantity": line.get("quantity", 0),
                    "unit_of_measure": line.get("unit_of_measure", ""),
                    "rate": rate,
                    "tonnes_per_1000": tonnes,
                    "cost_per_1000": cost,
                    "article_no": article,
                    "source": source,
                }
            )

        materials_total = board_total + other_total
        weighted_rate = board_total / board_tonnes if board_tonnes else 0.0
        summary = {
            "materials_cost_per_1000": round(materials_total, 4),
            "board_item_code": " | ".join(board_items),
            "board_article_code": " | ".join(articles),
            "board_price_per_tonne": round(weighted_rate, 4),
            "board_price_period": " | ".join(periods),
            "board_price_source": " | ".join(sources) or "No BOM board component",
            "board_tonnes_per_1000": round(board_tonnes, 6),
            "board_cost_per_1000": round(board_total, 4),
            "other_components_cost_per_1000": round(other_total, 4),
            "material_cost_source": "Automatic board and BOM component calculation",
            **machine_time,
        }
        return {"summary": summary, "lines": pd.DataFrame(detail_rows)}

    def material_breakdown(self, item_code: str) -> dict[str, Any]:
        return self._material_breakdown_from_frames(
            item_code, self.load_bom_lines(), self.load_board_items()
        )

    def machine_time_summary(self, item_code: str) -> dict[str, Any]:
        return self._machine_time_from_frames(item_code, self.load_bom_lines())

    def load_material_summary(self) -> pd.DataFrame:
        bom = self.load_bom_lines()
        boards = self.load_board_items()
        if bom.empty:
            return pd.DataFrame(columns=["item_code", *COST_INPUT_COLUMNS])
        summaries = []
        for item_code in bom["bom_code"].dropna().astype(str).unique():
            result = self._material_breakdown_from_frames(item_code, bom, boards)
            summaries.append({"item_code": item_code, **result["summary"]})
        return pd.DataFrame(summaries)

    def load_priced_board_catalog(self) -> pd.DataFrame:
        boards = self.load_board_items().copy()
        if boards.empty:
            return boards
        price = pd.to_numeric(boards["price_per_tonne"], errors="coerce")
        width = pd.to_numeric(boards["board_width_mm"], errors="coerce").fillna(
            pd.to_numeric(boards.get("resolved_width_mm"), errors="coerce")
        )
        length = pd.to_numeric(boards["board_length_mm"], errors="coerce").fillna(
            pd.to_numeric(boards.get("resolved_length_mm"), errors="coerce")
        )
        gsm = pd.to_numeric(boards["board_gsm"], errors="coerce").fillna(
            pd.to_numeric(boards.get("resolved_gsm"), errors="coerce")
        )
        boards["effective_width_mm"] = width
        boards["effective_length_mm"] = length
        boards["effective_gsm"] = gsm
        return boards[(price > 0) & width.gt(0) & length.gt(0) & gsm.gt(0)].copy()

    def new_item_material_breakdown(
        self,
        board_item_code: str,
        *,
        units_out: float,
        component_template_item_code: str = "",
    ) -> dict[str, Any]:
        if units_out <= 0:
            raise ValueError("Units out per board sheet must be greater than zero.")
        catalog = self.load_priced_board_catalog()
        selected = catalog[catalog["board_item_code"].astype(str) == str(board_item_code)]
        if selected.empty:
            raise ValueError("Choose a board item with an unambiguous Apr-26 mill price.")
        board = selected.iloc[0]
        tonnes = (
            float(board["effective_width_mm"])
            * float(board["effective_length_mm"])
            * float(board["effective_gsm"])
            / 1_000_000_000
            / units_out
        )
        rate = float(board["price_per_tonne"])
        board_cost = tonnes * rate

        other_lines = pd.DataFrame()
        if component_template_item_code:
            template = self.material_breakdown(component_template_item_code)
            template_lines = template["lines"]
            if not template_lines.empty:
                other_lines = template_lines[
                    template_lines["component_type"].eq("Other component")
                ].copy()
        other_total = (
            float(pd.to_numeric(other_lines.get("cost_per_1000", 0), errors="coerce").fillna(0).sum())
            if not other_lines.empty
            else 0.0
        )
        machine_time = (
            self.machine_time_summary(component_template_item_code)
            if component_template_item_code
            else {
                "machine_hours_per_1000": 0.0,
                "machine_time_source": "No comparable BOM machine-time profile",
            }
        )
        board_line = pd.DataFrame(
            [
                {
                    "component_type": "Board",
                    "component_code": board_item_code,
                    "description": board.get("board_item_name", ""),
                    "quantity": 1 / units_out,
                    "unit_of_measure": "1000 sheets",
                    "rate": rate,
                    "tonnes_per_1000": tonnes,
                    "cost_per_1000": board_cost,
                    "article_no": board.get("resolved_article_no", ""),
                    "source": board.get("price_source", ""),
                }
            ]
        )
        summary = {
            "materials_cost_per_1000": round(board_cost + other_total, 4),
            "board_item_code": board_item_code,
            "board_article_code": board.get("resolved_article_no", ""),
            "board_price_per_tonne": round(rate, 4),
            "board_price_period": board.get("price_period", ""),
            "board_price_source": board.get("price_source", ""),
            "board_tonnes_per_1000": round(tonnes, 6),
            "board_cost_per_1000": round(board_cost, 4),
            "other_components_cost_per_1000": round(other_total, 4),
            "component_template_item_code": component_template_item_code,
            "units_out": units_out,
            "material_cost_source": "Selected board plus automatic component template",
            **machine_time,
        }
        return {
            "summary": summary,
            "lines": pd.concat([board_line, other_lines], ignore_index=True, sort=False),
        }

    def load_current_items(self) -> pd.DataFrame:
        if not self.items_path.exists():
            return pd.DataFrame(columns=SPECIFICATION_COLUMNS)
        items = pd.read_csv(self.items_path)
        items = items.merge(self.load_bom_summary(), on="item_code", how="left")
        items = items.merge(self.load_material_summary(), on="item_code", how="left")
        defaults: dict[str, Any] = {
            "customer_name": "",
            "material": "BOM-defined materials",
            "fulfilment_type": "MTO",
            "quantity_input_mode": "Units",
            "order_quantity": 0,
            "order_pallets": 0,
            "agreement_term_months": 12,
            "delivery_pallets_per_calloff": 0,
            "estimated_delivery_count": 1,
            "pallet_holding_charge_per_pallet_per_week": 0.0,
            "delivery_postcode": "",
            "delivery_method": "Haulier",
            "transport_service": "Economy",
            "transport_vendor_preference": "Cheapest available",
            "transport_vendor": "",
            "transport_booking": "Standard",
            "transport_rate_zone": "",
            "transport_manual_override": 0,
            "manual_adjustment_per_1000": 0.0,
            "machine_hours_per_1000": 0.0,
            "machine_time_source": "No BOM machine-time profile",
            "spread_percent": 30.0,
            "units_out": 1.0,
        }
        for column, value in defaults.items():
            if column not in items:
                items[column] = value
            else:
                items[column] = items[column].fillna(value)
        for column in COST_INPUT_COLUMNS:
            if column not in items:
                items[column] = 0.0 if column in NUMERIC_COST_INPUT_COLUMNS else ""
        for column in NUMERIC_COST_INPUT_COLUMNS:
            items[column] = pd.to_numeric(items[column], errors="coerce").fillna(0)
        for column in set(COST_INPUT_COLUMNS) - set(NUMERIC_COST_INPUT_COLUMNS):
            items[column] = items[column].fillna("")
        return items

    def load_history(self) -> pd.DataFrame:
        if not self.history_path.exists() or self.history_path.stat().st_size == 0:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        history = pd.read_csv(self.history_path)
        for column in HISTORY_COLUMNS:
            if column not in history:
                history[column] = None
        return history[HISTORY_COLUMNS]

    def load_catalog(self) -> pd.DataFrame:
        """Return the feed plus the latest saved revision for each item code."""
        feed = self.load_current_items().copy()
        feed["source_type"] = "Current-item feed"

        history = self.load_history()
        if history.empty:
            return feed
        latest = (
            history.sort_values(["created_at_utc", "revision"])
            .groupby("item_code", as_index=False)
            .tail(1)
        )
        catalog_columns = [
            *SPECIFICATION_COLUMNS,
            *COST_INPUT_COLUMNS,
            "spread_percent",
        ]
        latest = latest[[c for c in catalog_columns if c in latest.columns]].copy()
        latest["source_type"] = "Saved costing"
        feed = feed[~feed["item_code"].isin(latest["item_code"])]
        return pd.concat([feed, latest], ignore_index=True, sort=False)

    def save_costing(
        self,
        record: dict[str, Any],
        *,
        user_email: str,
        user_name: str,
    ) -> dict[str, Any]:
        lock = FileLock(str(self.history_path) + ".lock", timeout=10)
        with lock:
            history = self.load_history()
            item_code = str(record.get("item_code", "")).strip()
            revisions = pd.to_numeric(
                history.loc[history["item_code"] == item_code, "revision"],
                errors="coerce",
            )
            revision = int(revisions.max()) + 1 if not revisions.empty else 1
            now = datetime.now(timezone.utc)
            saved = {
                **record,
                "costing_id": f"C-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
                "revision": revision,
                "created_at_utc": now.isoformat(timespec="seconds"),
                "created_by": user_email,
                "created_by_name": user_name,
            }
            row = pd.DataFrame(
                [{column: saved.get(column, "") for column in HISTORY_COLUMNS}]
            )
            updated = pd.concat([history, row], ignore_index=True)
            self._atomic_csv_write(updated, self.history_path)
        return saved

    @staticmethod
    def _atomic_csv_write(frame: pd.DataFrame, path: Path) -> None:
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            frame.to_csv(temp_path, index=False)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def data_directory(project_root: Path) -> Path:
    configured = os.getenv("COSTING_DATA_DIR")
    return Path(configured).expanduser() if configured else project_root / "data"
