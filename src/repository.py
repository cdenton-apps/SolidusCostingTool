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
    "description",
    "material",
    "product_group",
    "board_gsm",
    "blank_length_mm",
    "blank_width_mm",
    "pallet_quantity",
    "order_quantity",
    "material_cost_per_tonne",
    "bom_cost_per_1000",
    "print_cost_per_1000",
    "conversion_cost_per_1000",
    "packing_cost_per_1000",
    "fixed_tooling_cost",
    "waste_percent",
    "delivery_postcode",
    "delivery_method",
    "transport_rate_per_pallet",
]

CALCULATION_COLUMNS = [
    "net_weight_kg_per_1000",
    "gross_weight_kg_per_1000",
    "pallet_count",
    "transport_total",
    "material_cost_per_1000",
    "tooling_cost_per_1000",
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
    *CALCULATION_COLUMNS,
]


class CsvRepository:
    """CSV-backed repository with append-only costing history."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.items_path = self.data_dir / "current_items.csv"
        self.bom_path = self.data_dir / "bom_costs.csv"
        self.history_path = self.data_dir / "saved_costings.csv"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load_current_items(self) -> pd.DataFrame:
        if not self.items_path.exists():
            return pd.DataFrame(columns=SPECIFICATION_COLUMNS)
        items = pd.read_csv(self.items_path)
        if self.bom_path.exists():
            bom = pd.read_csv(self.bom_path)
            if not bom.empty and {"item_code", "cost_per_1000"}.issubset(bom.columns):
                totals = (
                    bom.groupby("item_code", as_index=False)["cost_per_1000"]
                    .sum()
                    .rename(columns={"cost_per_1000": "bom_cost_per_1000"})
                )
                items = items.drop(columns=["bom_cost_per_1000"], errors="ignore")
                items = items.merge(totals, on="item_code", how="left")
        if "bom_cost_per_1000" not in items:
            items["bom_cost_per_1000"] = 0.0
        items["bom_cost_per_1000"] = items["bom_cost_per_1000"].fillna(0)
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
        catalog_columns = [*SPECIFICATION_COLUMNS, "preferred_margin_percent"]
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
            row = pd.DataFrame([{column: saved.get(column, "") for column in HISTORY_COLUMNS}])
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
