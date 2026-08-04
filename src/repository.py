from __future__ import annotations

import os
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
    "order_quantity",
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
    "print_machine_cost_per_1000",
    "die_cut_machine_cost_per_1000",
    "fold_glue_machine_cost_per_1000",
    "other_machine_cost_per_1000",
    "labour_cost_per_1000",
    "manual_adjustment_per_1000",
    "fixed_tooling_cost",
]

CALCULATION_COLUMNS = [
    "net_weight_kg_per_1000",
    "pallet_count",
    "transport_total",
    "machine_cost_per_1000",
    "tooling_cost_per_1000",
    "manufacturing_cost_per_1000",
    "transport_cost_per_1000",
    "total_cost_per_1000",
    "cost_per_item",
    "preferred_margin_percent",
    "selling_price_per_1000",
    "selling_price_per_item",
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
    "bom_materials": "materials_cost_per_1000",
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

    def load_current_items(self) -> pd.DataFrame:
        if not self.items_path.exists():
            return pd.DataFrame(columns=SPECIFICATION_COLUMNS)
        items = pd.read_csv(self.items_path)
        items = items.merge(self.load_bom_summary(), on="item_code", how="left")
        defaults: dict[str, Any] = {
            "customer_name": "",
            "material": "BOM-defined materials",
            "order_quantity": 0,
            "delivery_postcode": "",
            "delivery_method": "Haulier",
            "transport_service": "Economy",
            "transport_vendor_preference": "Cheapest available",
            "transport_vendor": "",
            "transport_booking": "Standard",
            "transport_rate_zone": "",
            "transport_manual_override": 0,
            "manual_adjustment_per_1000": 0.0,
            "fixed_tooling_cost": 0.0,
            "preferred_margin_percent": 30.0,
        }
        for column, value in defaults.items():
            if column not in items:
                items[column] = value
            else:
                items[column] = items[column].fillna(value)
        for column in COST_INPUT_COLUMNS:
            if column not in items:
                items[column] = 0.0
            items[column] = pd.to_numeric(items[column], errors="coerce").fillna(0)
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
            "preferred_margin_percent",
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

