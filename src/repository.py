from __future__ import annotations

import ast
import hashlib
import os
import logging
import math
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from filelock import FileLock, Timeout
import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


LOGGER = logging.getLogger(__name__)


def _safe_integer(value: Any, default: int = 0) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if math.isfinite(number) else default


class RepositoryBusyError(RuntimeError):
    """Raised when another user's save holds the costing-history lock."""


BOARD_CODE_PATTERN = re.compile(r"(?:SHT\d+(?:/[A-Z])?|[234]-\d+)", re.IGNORECASE)
BOARD_FIT_MARGIN_MM = 10.0


def board_material_spec(board_description: Any, fallback: Any = "") -> str:
    """Return the board grade/material layers embedded in its description.

    Stock descriptions normally use ``BOARD1360X876/1000GSM/KL/TKL.WPE``.
    A smaller number put the GSM at the end, so both layouts are supported.
    Article numbers and short Sage item codes are deliberately not treated as
    material descriptions.
    """

    text = str(board_description or "").strip().strip("/")
    if not text:
        return str(fallback or "").strip()
    parts = [part.strip() for part in text.split("/") if part.strip()]
    gsm_index = next(
        (
            index
            for index, part in enumerate(parts)
            if re.fullmatch(r"\d+(?:\.\d+)?\s*G(?:SM)?", part, re.IGNORECASE)
        ),
        None,
    )
    if gsm_index is None:
        return str(fallback or "").strip()

    after_gsm = parts[gsm_index + 1 :]
    if after_gsm:
        return "/".join(after_gsm)

    # Some legacy descriptions are BOARD<dimensions>/<material>/<material>/<gsm>.
    dimension_index = next(
        (
            index
            for index, part in enumerate(parts[:gsm_index])
            if re.search(r"\d+\s*[Xx]\s*\d+", part)
        ),
        -1,
    )
    before_gsm = parts[dimension_index + 1 : gsm_index]
    return "/".join(before_gsm) or str(fallback or "").strip()


def flat_net_dimensions(
    finished_length_mm: Any,
    finished_width_mm: Any,
    finished_height_mm: Any,
) -> tuple[float, float]:
    """Return a tray-style starting estimate for the complete flat net.

    The estimate adds both side walls to both finished-plan dimensions.  The
    UI exposes the result as editable net/blank dimensions because the final
    CAD or forme footprint remains authoritative for non-standard structures.
    """

    try:
        length = float(finished_length_mm)
        width = float(finished_width_mm)
        height = float(finished_height_mm)
    except (TypeError, ValueError):
        return 0.0, 0.0
    values = (length, width, height)
    if any(not math.isfinite(value) or value <= 0 for value in values):
        return 0.0, 0.0
    return length + (2 * height), width + (2 * height)


def board_fit_layout(
    net_length_mm: Any,
    net_width_mm: Any,
    board_length_mm: Any,
    board_width_mm: Any,
    *,
    margin_mm: float = BOARD_FIT_MARGIN_MM,
) -> dict[str, int]:
    """Return the best same-orientation rectangular x-up layout for a flat net.

    A margin is retained at every outside edge and between adjacent nets.  Both
    the net and sheet orientations are evaluated.  Unlike the earlier helper,
    this is not capped at 2-up.
    """

    try:
        net = (float(net_length_mm), float(net_width_mm))
        sheet = (float(board_length_mm), float(board_width_mm))
        margin = max(0.0, float(margin_mm))
    except (TypeError, ValueError):
        return {"units": 0, "across": 0, "down": 0}
    if any(not math.isfinite(value) or value <= 0 for value in (*net, *sheet)):
        return {"units": 0, "across": 0, "down": 0}

    best = {"units": 0, "across": 0, "down": 0}
    for net_length, net_width in (net, (net[1], net[0])):
        for sheet_length, sheet_width in (sheet, (sheet[1], sheet[0])):
            usable_length = sheet_length - (2 * margin)
            usable_width = sheet_width - (2 * margin)
            if usable_length <= 0 or usable_width <= 0:
                continue
            down = max(0, math.floor((usable_length + margin) / (net_length + margin)))
            across = max(0, math.floor((usable_width + margin) / (net_width + margin)))
            units = across * down
            if units > best["units"]:
                best = {"units": units, "across": across, "down": down}
    return best


def board_fit_units(
    net_length_mm: Any,
    net_width_mm: Any,
    board_length_mm: Any,
    board_width_mm: Any,
    *,
    margin_mm: float = BOARD_FIT_MARGIN_MM,
) -> int:
    """Return the maximum x-up count for the complete flat net."""

    return board_fit_layout(
        net_length_mm,
        net_width_mm,
        board_length_mm,
        board_width_mm,
        margin_mm=margin_mm,
    )["units"]


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
    "net_length_mm",
    "net_width_mm",
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
    "delivered_to",
    "delivery_method",
    "incoterm",
    "transport_service",
    "transport_vendor_preference",
    "transport_vendor",
    "transport_booking",
    "transport_rate_zone",
    "transport_manual_override",
    "annual_volume_band",
    "annual_volume_units",
    "comex_consistent_payer",
    "comex_strategic_customer",
    "comex_over_credit_limit",
    "comex_poor_payment_history",
    "quote_currency",
    "eur_per_gbp",
    "eur_rate_date",
    "eur_rate_source",
]

COST_INPUT_COLUMNS = [
    "bom_available",
    "materials_cost_per_1000",
    "board_item_code",
    "board_article_code",
    "board_price_per_tonne",
    "board_price_period",
    "board_price_source",
    "board_material_spec",
    "board_tonnes_per_1000",
    "board_cost_per_1000",
    "other_components_cost_per_1000",
    "component_template_item_code",
    "units_out",
    "new_board_required",
    "new_board_item_code",
    "new_board_material_spec",
    "new_board_price_per_tonne",
    "print_operations_included",
    "material_cost_source",
    "machine_hours_per_1000",
    "machine_time_source",
    "annual_volume_adjustment_percent",
    "comex_adjustment_percent",
    "total_material_adjustment_percent",
    "material_adjustment_value_per_1000",
    "adjusted_materials_cost_per_1000",
]

NUMERIC_COST_INPUT_COLUMNS = [
    "bom_available",
    "materials_cost_per_1000",
    "board_price_per_tonne",
    "board_tonnes_per_1000",
    "board_cost_per_1000",
    "other_components_cost_per_1000",
    "units_out",
    "new_board_required",
    "new_board_price_per_tonne",
    "print_operations_included",
    "machine_hours_per_1000",
    "annual_volume_adjustment_percent",
    "comex_adjustment_percent",
    "total_material_adjustment_percent",
    "material_adjustment_value_per_1000",
    "adjusted_materials_cost_per_1000",
]

CALCULATION_COLUMNS = [
    "net_weight_kg_per_1000",
    "pallet_count",
    "transport_total",
    "material_base_per_1000",
    "transport_cost_per_1000",
    "tooling_amortisation_per_1000",
    "pricing_base_per_1000",
    "pricing_base_per_item",
    "spread_percent",
    "spread_value_per_1000",
    "selling_price_per_1000",
    "selling_price_per_item",
    "material_spread_value_per_1000",
    "total_machine_hours",
    "total_spread_value",
    "spread_per_machine_hour",
    "traffic_light_status",
    "traffic_light_reason",
    "traffic_override_approved",
    "traffic_override_reason",
    "traffic_override_by_username",
    "traffic_override_by_name",
    "traffic_override_by_email",
    "traffic_override_at_utc",
    "traffic_override_basis",
    "traffic_amber_acknowledged",
    "traffic_amber_acknowledged_by_username",
    "traffic_amber_acknowledged_at_utc",
    "traffic_amber_acknowledgement_basis",
]

HISTORY_COLUMNS = [
    "costing_id",
    "revision",
    "source_item_code",
    "catalogue_product",
    "created_at_utc",
    "created_by",
    "created_by_username",
    "created_by_name",
    "quote_reference",
    "quote_number",
    "quote_revision",
    "customer_contact",
    "customer_role",
    "customer_email",
    "director_name",
    "director_email",
    "approval_recipient_name",
    "approval_recipient_email",
    "approval_recipient_role",
    "approval_recipient_is_cover",
    "sales_rep_signature_id",
    "sales_rep_signature_name",
    "sales_rep_signature_applied_at_utc",
    "sales_rep_signature_sha256",
    "notes",
    "additional_charge_description",
    "additional_charge_amount",
    "additional_charge_foc",
    "esign_request_id",
    "esign_status",
    "esign_is_complete",
    "esign_is_declined",
    "esign_signers",
    "esign_test_mode",
    "esign_approved_by_username",
    "esign_approved_by_name",
    "esign_approved_by_email",
    "esign_approved_at_utc",
    "esign_internal_signer_role",
    "is_multi_item_quote",
    "quote_items",
    "multi_delivery_mode",
    "quoted_value",
    "annual_revenue",
    *SPECIFICATION_COLUMNS,
    *COST_INPUT_COLUMNS,
    *CALCULATION_COLUMNS,
]

SESSION_COLUMNS = [
    "session_id",
    "username",
    "name",
    "email",
    "signed_in_at_utc",
    "last_activity_utc",
    "last_heartbeat_utc",
    "active_seconds",
    "current_page",
    "force_logout",
    "ended_at_utc",
]

APP_USER_ROLES = {"external", "creator", "admin"}

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
    """CSV reference feeds with optional Neon-backed history and sessions."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        database_url: str | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.items_path = self.data_dir / "current_items.csv"
        compressed_bom_path = self.data_dir / "bom_costs.csv.gz"
        self.bom_path = (
            compressed_bom_path
            if compressed_bom_path.exists()
            else self.data_dir / "bom_costs.csv"
        )
        self.board_items_path = self.data_dir / "board_items.csv"
        self.board_prices_path = self.data_dir / "board_prices.csv"
        self.haulier_path = self.data_dir / "haulier_rates.csv"
        self.material_summary_path = self.data_dir / "material_summaries.csv"
        self.history_path = self.data_dir / "saved_costings.csv"
        self.sessions_path = self.data_dir / "active_sessions.csv"
        self.database_url = str(database_url or "").strip()
        self.uses_database = bool(self.database_url)
        self._pool: ConnectionPool | None = None
        self._pool_lock = threading.Lock()
        self._reference_frames: dict[Path, tuple[tuple[int, int], pd.DataFrame]] = {}
        self._derived_frames: dict[str, tuple[Any, pd.DataFrame]] = {}
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        if not self.database_url:
            raise RuntimeError("Database storage is not configured.")
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        conninfo=self.database_url,
                        min_size=0,
                        max_size=4,
                        max_idle=60,
                        timeout=10,
                        kwargs={
                            "connect_timeout": 10,
                            "row_factory": dict_row,
                        },
                        open=True,
                    )
        return self._pool.connection(timeout=10)

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        if not path.exists():
            return (0, 0)
        status = path.stat()
        return (status.st_mtime_ns, status.st_size)

    def reference_data_version(self) -> tuple[tuple[int, int], ...]:
        """Return a cheap cache key that changes when an input feed changes."""
        return tuple(
            self._file_signature(path)
            for path in (
                self.items_path,
                self.bom_path,
                self.board_items_path,
                self.board_prices_path,
                self.material_summary_path,
                self.haulier_path,
            )
        )

    def _read_reference_csv(self, path: Path) -> pd.DataFrame:
        """Read a fixed input feed once, refreshing automatically after replacement."""
        if not path.exists():
            return pd.DataFrame()
        signature = self._file_signature(path)
        cached = self._reference_frames.get(path)
        if cached is None or cached[0] != signature:
            cached = (signature, pd.read_csv(path))
            self._reference_frames[path] = cached
            self._derived_frames.clear()
        return cached[1].copy()

    @staticmethod
    def _json_ready(value: Any) -> Any:
        """Convert pandas/numpy values into safe JSON values for Postgres."""
        if isinstance(value, dict):
            return {
                str(key): CsvRepository._json_ready(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [CsvRepository._json_ready(item) for item in value]
        if isinstance(value, (datetime, pd.Timestamp)):
            return value.isoformat()
        if hasattr(value, "item"):
            value = value.item()
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _timestamp(value: Any, *, fallback: datetime | None = None) -> datetime | None:
        if value in (None, ""):
            return fallback
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        return parsed.to_pydatetime() if pd.notna(parsed) else fallback

    def has_app_users(self) -> bool:
        """Return whether database-backed login has been populated."""
        if not self.uses_database:
            return False
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM public.app_users) AS present"
                    )
                    row = cursor.fetchone()
            return bool(row and row["present"])
        except psycopg.errors.UndefinedTable:
            return False
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The user database could not be checked. Please try again."
            ) from exc

    def get_app_user(self, username: str) -> dict[str, Any] | None:
        if not self.uses_database or not str(username or "").strip():
            return None
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT username, email, name, password_hash, role, "
                        "can_view_history, is_active, must_change_password, "
                        "session_version, created_at_utc, updated_at_utc, last_login_at_utc "
                        "FROM public.app_users WHERE lower(username) = lower(%s)",
                        (str(username).strip(),),
                    )
                    return cursor.fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The user database could not be checked. Please try again."
            ) from exc

    def app_user_login_security(self, username: str) -> dict[str, Any]:
        """Return recent failed-attempt and temporary-lock information."""
        status = {
            "failed_attempts": 0,
            "is_locked": False,
            "locked_until_utc": None,
        }
        if not self.uses_database or not str(username or "").strip():
            return status
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "WITH reset AS ("
                        " SELECT max(occurred_at_utc) AS occurred_at_utc "
                        " FROM public.app_audit_log "
                        " WHERE lower(target_username) = lower(%s) "
                        " AND action IN ('login_success', 'login_unlocked', "
                        "'password_changed')"
                        "), failures AS ("
                        " SELECT count(*)::integer AS failed_attempts, "
                        "max(log.occurred_at_utc) AS last_failed_at_utc "
                        " FROM public.app_audit_log AS log CROSS JOIN reset "
                        " WHERE lower(log.target_username) = lower(%s) "
                        " AND log.action = 'login_failed' "
                        " AND log.occurred_at_utc >= now() - interval '15 minutes' "
                        " AND (reset.occurred_at_utc IS NULL OR "
                        "log.occurred_at_utc > reset.occurred_at_utc)"
                        ") SELECT failed_attempts, "
                        "CASE WHEN failed_attempts >= 5 "
                        "THEN last_failed_at_utc + interval '15 minutes' END "
                        "AS locked_until_utc FROM failures",
                        (str(username).strip(), str(username).strip()),
                    )
                    row = cursor.fetchone() or {}
            failed_attempts = int(row.get("failed_attempts", 0) or 0)
            locked_until = self._timestamp(row.get("locked_until_utc"))
            return {
                "failed_attempts": failed_attempts,
                "is_locked": bool(
                    failed_attempts >= 5
                    and locked_until is not None
                    and locked_until > datetime.now(timezone.utc)
                ),
                "locked_until_utc": locked_until,
            }
        except psycopg.errors.UndefinedTable:
            return status
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The login security status could not be checked. Please try again."
            ) from exc

    def record_login_failure(self, username: str) -> dict[str, Any]:
        """Audit a failed password attempt and return the resulting lock status."""
        if not self.uses_database:
            return {
                "failed_attempts": 0,
                "is_locked": False,
                "locked_until_utc": None,
            }
        username = str(username or "").strip()
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"app-login:{username.casefold()}",),
                    )
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, 'login_failed', %s, '{}'::jsonb)",
                        (username or "unknown", username or "unknown"),
                    )
            status = self.app_user_login_security(username)
            if status["is_locked"] and status["failed_attempts"] == 5:
                self._record_app_audit(
                    username or "unknown",
                    "login_locked",
                    username or "unknown",
                    {"lock_minutes": 15},
                )
            return status
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The login attempt could not be checked. Please try again."
            ) from exc

    def unlock_app_user(self, username: str, *, actor_username: str) -> None:
        """Clear a temporary login lock by recording an administrative reset."""
        if not self.uses_database:
            raise RepositoryBusyError("Neon must be configured to unlock users.")
        self._record_app_audit(
            actor_username,
            "login_unlocked",
            str(username).strip(),
            {},
        )

    def _record_app_audit(
        self,
        actor_username: str,
        action: str,
        target_username: str,
        detail: dict[str, Any],
    ) -> None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, %s, %s, %s)",
                        (
                            str(actor_username).strip() or "system",
                            str(action).strip(),
                            str(target_username).strip(),
                            Jsonb(detail),
                        ),
                    )
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The user audit could not be recorded. Please try again."
            ) from exc

    def list_app_users(self) -> pd.DataFrame:
        columns = [
            "username", "email", "name", "role", "can_view_history",
            "is_active", "must_change_password", "created_at_utc",
            "updated_at_utc", "last_login_at_utc",
        ]
        if not self.uses_database:
            return pd.DataFrame(columns=columns)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT username, email, name, role, can_view_history, "
                        "is_active, must_change_password, created_at_utc, "
                        "updated_at_utc, last_login_at_utc FROM public.app_users "
                        "ORDER BY lower(name), lower(username)"
                    )
                    rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns)
        except psycopg.errors.UndefinedTable:
            return pd.DataFrame(columns=columns)
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The user list could not be loaded. Please try again."
            ) from exc

    def get_active_user_signature(self, username: str) -> dict[str, Any] | None:
        """Return only the active signature owned by the supplied username."""
        if not self.uses_database or not str(username or "").strip():
            return None
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT signature_id, username, image_png, image_sha256, "
                        "created_at_utc FROM public.user_signatures "
                        "WHERE lower(username) = lower(%s) "
                        "AND revoked_at_utc IS NULL ORDER BY created_at_utc DESC LIMIT 1",
                        (str(username).strip(),),
                    )
                    row = cursor.fetchone()
            return dict(row) if row else None
        except psycopg.errors.UndefinedTable:
            return None
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "Your saved signature could not be loaded. Please try again."
            ) from exc

    def get_user_signature_version(
        self,
        signature_id: str,
        *,
        expected_username: str,
    ) -> dict[str, Any] | None:
        """Load one immutable signature version only for its recorded owner."""
        if (
            not self.uses_database
            or not str(signature_id or "").strip()
            or not str(expected_username or "").strip()
        ):
            return None
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT signature_id, username, image_png, image_sha256, "
                        "created_at_utc FROM public.user_signatures "
                        "WHERE signature_id = %s AND lower(username) = lower(%s)",
                        (str(signature_id).strip(), str(expected_username).strip()),
                    )
                    row = cursor.fetchone()
            return dict(row) if row else None
        except psycopg.errors.UndefinedTable:
            return None
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The quotation signature could not be loaded. Please try again."
            ) from exc

    def save_user_signature(
        self,
        username: str,
        image_png: bytes,
        *,
        actor_username: str,
    ) -> dict[str, Any]:
        """Replace a user's signature without allowing cross-account writes."""
        username = str(username or "").strip()
        actor_username = str(actor_username or "").strip()
        if username.casefold() != actor_username.casefold():
            raise ValueError("You can only change the signature on your own account.")
        content = bytes(image_png or b"")
        if not content or len(content) > 1_000_000:
            raise ValueError("The processed signature image is not a safe size.")
        signature_id = f"SIG-{uuid.uuid4().hex.upper()}"
        digest = hashlib.sha256(content).hexdigest()
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"user-signature:{username.casefold()}",),
                    )
                    cursor.execute(
                        "UPDATE public.user_signatures SET revoked_at_utc = now() "
                        "WHERE lower(username) = lower(%s) AND revoked_at_utc IS NULL",
                        (username,),
                    )
                    cursor.execute(
                        "INSERT INTO public.user_signatures "
                        "(signature_id, username, image_png, image_sha256, created_by) "
                        "SELECT %s, username, %s, %s, %s FROM public.app_users "
                        "WHERE lower(username) = lower(%s) RETURNING signature_id, "
                        "username, image_png, image_sha256, created_at_utc",
                        (signature_id, content, digest, actor_username, username),
                    )
                    row = cursor.fetchone()
                    if not row:
                        raise ValueError("Your user account could not be found.")
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, 'signature_saved', %s, %s)",
                        (actor_username, username, Jsonb({"signature_id": signature_id})),
                    )
            return dict(row)
        except psycopg.errors.UndefinedTable as exc:
            raise RepositoryBusyError(
                "Signature storage is not ready in Neon. Run the latest schema update first."
            ) from exc
        except (psycopg.Error, TypeError) as exc:
            raise RepositoryBusyError(
                "Your signature could not be saved. Please try again."
            ) from exc

    def remove_user_signature(
        self,
        username: str,
        *,
        actor_username: str,
    ) -> None:
        """Revoke the active signature without affecting saved quotation versions."""
        username = str(username or "").strip()
        actor_username = str(actor_username or "").strip()
        if username.casefold() != actor_username.casefold():
            raise ValueError("You can only remove the signature on your own account.")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE public.user_signatures SET revoked_at_utc = now() "
                        "WHERE lower(username) = lower(%s) AND revoked_at_utc IS NULL",
                        (username,),
                    )
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, 'signature_removed', %s, '{}'::jsonb)",
                        (actor_username, username),
                    )
        except psycopg.errors.UndefinedTable as exc:
            raise RepositoryBusyError(
                "Signature storage is not ready in Neon. Run the latest schema update first."
            ) from exc
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "Your signature could not be removed. Please try again."
            ) from exc

    def save_app_user(
        self,
        *,
        username: str,
        email: str,
        name: str,
        password_hash: str | None,
        role: str,
        can_view_history: bool,
        is_active: bool,
        must_change_password: bool,
        actor_username: str,
    ) -> None:
        """Create or update an app user and retain an audit entry."""
        if not self.uses_database:
            raise RepositoryBusyError("Neon must be configured to manage users.")
        username = str(username or "").strip()
        email = str(email or "").strip()
        name = str(name or "").strip()
        role = str(role or "external").strip().lower()
        if not username or not email or not name:
            raise ValueError("Username, name and email are required.")
        if role not in APP_USER_ROLES:
            raise ValueError("The selected user role is not supported.")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT username FROM public.app_users "
                        "WHERE lower(username) = lower(%s)",
                        (username,),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        if password_hash:
                            cursor.execute(
                                "UPDATE public.app_users SET email = %s, name = %s, "
                                "role = %s, can_view_history = %s, is_active = %s, "
                                "must_change_password = %s, password_hash = %s, "
                                "password_changed_at_utc = now(), "
                                "session_version = session_version + 1, "
                                "updated_at_utc = now() "
                                "WHERE lower(username) = lower(%s)",
                                (
                                    email, name, role, bool(can_view_history),
                                    bool(is_active), bool(must_change_password),
                                    password_hash, username,
                                ),
                            )
                        else:
                            cursor.execute(
                                "UPDATE public.app_users SET email = %s, name = %s, "
                                "role = %s, can_view_history = %s, is_active = %s, "
                                "session_version = CASE WHEN is_active AND NOT %s "
                                "THEN session_version + 1 ELSE session_version END, "
                                "updated_at_utc = now() "
                                "WHERE lower(username) = lower(%s)",
                                (
                                    email, name, role, bool(can_view_history),
                                    bool(is_active), bool(is_active), username,
                                ),
                            )
                        action = "user_updated"
                    else:
                        if not password_hash:
                            raise ValueError("A password is required for a new user.")
                        cursor.execute(
                            "INSERT INTO public.app_users "
                            "(username, email, name, password_hash, role, "
                            "can_view_history, is_active, must_change_password, created_by) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                username, email, name, password_hash, role,
                                bool(can_view_history), bool(is_active),
                                bool(must_change_password), actor_username,
                            ),
                        )
                        action = "user_created"
                    if not is_active:
                        cursor.execute(
                            "UPDATE public.app_sessions SET force_logout = true "
                            "WHERE lower(username) = lower(%s) AND ended_at_utc IS NULL",
                            (username,),
                        )
                    elif password_hash:
                        cursor.execute(
                            "UPDATE public.app_sessions SET force_logout = true "
                            "WHERE lower(username) = lower(%s) AND ended_at_utc IS NULL",
                            (username,),
                        )
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, %s, %s, %s)",
                        (
                            actor_username, action, username,
                            Jsonb({
                                "role": role,
                                "can_view_history": bool(can_view_history),
                                "is_active": bool(is_active),
                                "password_changed": bool(password_hash),
                            }),
                        ),
                    )
        except (psycopg.Error, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            LOGGER.exception(
                "Could not save app user %s (sqlstate=%s)",
                username,
                getattr(exc, "sqlstate", None),
            )
            raise RepositoryBusyError(
                "The user could not be saved. Please try again."
            ) from exc

    def import_app_users(
        self,
        users: list[dict[str, Any]],
        *,
        actor_username: str,
    ) -> tuple[int, int]:
        """Import missing Secrets users without overwriting database users."""
        if not self.uses_database:
            raise RepositoryBusyError("Neon must be configured to import users.")
        imported = 0
        for user in users:
            if self.get_app_user(str(user.get("username", ""))):
                continue
            self.save_app_user(
                username=str(user.get("username", "")),
                email=str(user.get("email", "")),
                name=str(user.get("name", "")),
                password_hash=str(user.get("password_hash", "")),
                role=str(user.get("role", "external")),
                can_view_history=bool(user.get("can_view_history", False)),
                is_active=True,
                must_change_password=bool(user.get("must_change_password", False)),
                actor_username=actor_username,
            )
            imported += 1
        return imported, len(users)

    def record_user_login(self, username: str) -> None:
        if not self.uses_database:
            return
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE public.app_users SET last_login_at_utc = now(), "
                        "updated_at_utc = now() WHERE lower(username) = lower(%s)",
                        (str(username).strip(),),
                    )
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, 'login_success', %s, '{}'::jsonb)",
                        (str(username).strip(), str(username).strip()),
                    )
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The login could not be recorded. Please try again."
            ) from exc

    def change_app_user_password(
        self,
        username: str,
        password_hash: str,
        *,
        actor_username: str,
    ) -> None:
        if not self.uses_database:
            raise RepositoryBusyError("Neon must be configured to change passwords.")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE public.app_users SET password_hash = %s, "
                        "must_change_password = false, password_changed_at_utc = now(), "
                        "session_version = session_version + 1, updated_at_utc = now() "
                        "WHERE lower(username) = lower(%s)",
                        (password_hash, str(username).strip()),
                    )
                    cursor.execute(
                        "UPDATE public.app_sessions SET force_logout = true "
                        "WHERE lower(username) = lower(%s) AND ended_at_utc IS NULL",
                        (str(username).strip(),),
                    )
                    cursor.execute(
                        "INSERT INTO public.app_audit_log "
                        "(actor_username, action, target_username, detail) "
                        "VALUES (%s, 'password_changed', %s, '{}'::jsonb)",
                        (actor_username, str(username).strip()),
                    )
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The password could not be changed. Please try again."
            ) from exc

    def load_app_audit_log(self, limit: int = 100) -> pd.DataFrame:
        columns = [
            "occurred_at_utc", "actor_username", "action",
            "target_username", "detail",
        ]
        if not self.uses_database:
            return pd.DataFrame(columns=columns)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT occurred_at_utc, actor_username, action, "
                        "target_username, detail FROM public.app_audit_log "
                        "ORDER BY occurred_at_utc DESC LIMIT %s",
                        (max(1, min(int(limit), 500)),),
                    )
                    rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns)
        except psycopg.errors.UndefinedTable:
            return pd.DataFrame(columns=columns)
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The user audit log could not be loaded. Please try again."
            ) from exc

    def load_bom_lines(self, item_code: str | None = None) -> pd.DataFrame:
        if not self.bom_path.exists():
            return pd.DataFrame()
        bom = self._read_reference_csv(self.bom_path)
        if item_code is not None and "bom_code" in bom:
            bom = bom[bom["bom_code"] == item_code]
        return bom

    def load_bom_summary(self) -> pd.DataFrame:
        signature = self._file_signature(self.bom_path)
        cached = self._derived_frames.get("bom_summary")
        if cached is not None and cached[0] == signature:
            return cached[1].copy()
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
        self._derived_frames["bom_summary"] = (signature, summary)
        return summary.copy()

    def load_board_items(self) -> pd.DataFrame:
        if not self.board_items_path.exists():
            return pd.DataFrame()
        return self._read_reference_csv(self.board_items_path)

    def load_board_prices(self) -> pd.DataFrame:
        if not self.board_prices_path.exists():
            return pd.DataFrame()
        return self._read_reference_csv(self.board_prices_path)

    def find_board_by_code(
        self,
        code: str,
        *,
        manufacturing_site: str = "",
    ) -> dict[str, Any] | None:
        """Find the best known board row for an article or stock code."""
        target = str(code or "").strip().rstrip("/").strip().upper()
        if not target:
            return None
        boards = self.load_board_items().copy()
        if boards.empty:
            return None

        def aliases(row: pd.Series) -> set[str]:
            values: set[str] = set()
            for field in [
                "board_item_code",
                "resolved_article_no",
                "board_code_raw",
                "legacy_code",
            ]:
                text = str(row.get(field, "") or "").strip().rstrip("/").strip().upper()
                if text and text != "NAN":
                    values.add(text)
                    values.update(match.group(0).upper() for match in BOARD_CODE_PATTERN.finditer(text))
            return values

        boards["_aliases"] = boards.apply(aliases, axis=1)
        matches = boards[boards["_aliases"].map(lambda values: target in values)].copy()
        if matches.empty:
            return None
        site = str(manufacturing_site or "").strip().split(".")[0]
        if site:
            site_matches = matches[
                matches["manufacturing_site"].fillna("").astype(str).str.split(".").str[0].eq(site)
            ]
            if not site_matches.empty:
                matches = site_matches
        matches["_priced"] = pd.to_numeric(
            matches.get("price_per_tonne"), errors="coerce"
        ).fillna(0).gt(0)
        matches["_plain"] = matches.apply(self._is_plain_board, axis=1)
        matches = matches.sort_values(
            ["_plain", "_priced", "board_item_code"],
            ascending=[False, False, True],
        )
        row = matches.iloc[0]
        article_value = row.get("resolved_article_no", "")
        article = (
            target
            if pd.isna(article_value)
            or str(article_value or "").strip().lower() in {"", "nan"}
            else str(article_value).strip().rstrip("/")
        )
        period_value = row.get("price_period", "")
        period = "" if pd.isna(period_value) else str(period_value or "")
        source_value = row.get("price_source", "")
        source = "" if pd.isna(source_value) else str(source_value or "")
        return {
            "board_item_code": str(row.get("board_item_code", "") or ""),
            "board_code": article,
            "board_gsm": self._positive_number(row.get("resolved_gsm"))
            or self._positive_number(row.get("board_gsm"))
            or 0.0,
            "board_width_mm": self._positive_number(row.get("resolved_width_mm"))
            or self._positive_number(row.get("board_width_mm"))
            or 0.0,
            "board_length_mm": self._positive_number(row.get("resolved_length_mm"))
            or self._positive_number(row.get("board_length_mm"))
            or 0.0,
            "fsc": str(row.get("fsc", "") or ""),
            "board_price_per_tonne": self._positive_number(row.get("price_per_tonne")) or 0.0,
            "board_price_period": period,
            "board_price_source": source,
            "board_item_name": str(row.get("board_item_name", "") or ""),
            "board_material_spec": board_material_spec(
                row.get("board_item_name", "")
            ),
            "material": board_material_spec(row.get("board_item_name", "")),
            "match_count": int(len(matches)),
        }

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @classmethod
    def _machine_time_details_from_frames(
        cls,
        item_code: str,
        bom: pd.DataFrame,
        *,
        include_print: bool = True,
    ) -> dict[str, Any]:
        """Calculate and explain machine hours per 1,000 from BOM speeds.

        Direct operations provide run hours and effective output per run. Rolled
        child machine rows do not carry the child speed, so their already-scaled
        machine value is divided by the matching imported machine rate only to
        recover time. No machine value is added to the commercial pricing base.
        """
        empty_summary = {
            "machine_hours_per_1000": 0.0,
            "machine_time_source": "No BOM machine-time profile",
        }
        if bom.empty or "bom_code" not in bom or "cost_type" not in bom:
            return {"summary": empty_summary, "lines": pd.DataFrame()}
        item_lines = bom[bom["bom_code"].astype(str) == str(item_code)].copy()
        if item_lines.empty:
            return {"summary": empty_summary, "lines": pd.DataFrame()}

        informational = pd.to_numeric(
            item_lines.get("is_informational_row", 0), errors="coerce"
        ).fillna(0)
        machine_lines = item_lines[
            item_lines["cost_type"].astype(str).eq("Machine")
            & informational.eq(0)
        ].copy()
        if not include_print and not machine_lines.empty:
            print_text = pd.Series("", index=machine_lines.index, dtype="object")
            for column in [
                "operation_description",
                "process_group",
                "machine_bucket",
                "cost_description",
            ]:
                if column in machine_lines:
                    print_text = print_text + " " + machine_lines[column].astype(str)
            machine_lines = machine_lines[
                ~print_text.str.contains("print", case=False, regex=False)
            ].copy()
        run_hours = pd.to_numeric(
            machine_lines.get("run_hours"), errors="coerce"
        )
        imported_effective_quantity = pd.to_numeric(
            machine_lines.get("effective_quantity_per_run"), errors="coerce"
        )
        system_quantity = pd.to_numeric(
            machine_lines.get("system_quantity_per_run"), errors="coerce"
        )
        effective_quantity = imported_effective_quantity.where(
            imported_effective_quantity.gt(0), system_quantity
        )
        valid_direct = run_hours.gt(0) & effective_quantity.gt(0)
        direct_calculated_hours = run_hours[valid_direct] / effective_quantity[valid_direct]
        direct_hours = float(direct_calculated_hours.sum())
        detail_rows: list[dict[str, Any]] = []
        for index in machine_lines.index[valid_direct]:
            line = machine_lines.loc[index]
            effective = float(effective_quantity.loc[index])
            run = float(run_hours.loc[index])
            used_column_q = bool(imported_effective_quantity.loc[index] > 0)
            detail_rows.append(
                {
                    "line_type": "Direct operation",
                    "operation": str(
                        line.get("operation_description")
                        or line.get("cost_description")
                        or line.get("process_group")
                        or "Machine operation"
                    ),
                    "machine": str(
                        line.get("cost_code") or line.get("machine_bucket") or ""
                    ),
                    "run_hours": run,
                    "system_quantity_per_run": system_quantity.loc[index],
                    "effective_quantity_per_run": effective,
                    "quantity_source": (
                        "Column Q — effective quantity"
                        if used_column_q
                        else "System quantity fallback"
                    ),
                    "calculation": f"{run:.6f} ÷ {effective:.6f}",
                    "hours_per_1000": float(direct_calculated_hours.loc[index]),
                }
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
                if not include_print:
                    continue
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
                recovered_hours = extended / rate
                rolled_hours += recovered_hours
                detail_rows.append(
                    {
                        "line_type": "Rolled child",
                        "operation": str(line.get("cost_description", "Machine time")),
                        "machine": bucket or "Matched median machine rate",
                        "run_hours": None,
                        "system_quantity_per_run": None,
                        "effective_quantity_per_run": None,
                        "quantity_source": "Recovered from rolled machine value",
                        "calculation": f"£{extended:.6f} ÷ £{rate:.6f}/hour",
                        "hours_per_1000": recovered_hours,
                    }
                )

        total_hours = direct_hours + rolled_hours
        if total_hours <= 0:
            source = "No BOM machine-time profile"
        elif rolled_hours > 0:
            source = "BOM operation speeds plus rolled-child machine time"
        else:
            source = "BOM operation speeds"
        return {
            "summary": {
                "machine_hours_per_1000": round(total_hours, 6),
                "machine_time_source": source,
            },
            "lines": pd.DataFrame(detail_rows),
        }

    @classmethod
    def _machine_time_from_frames(
        cls,
        item_code: str,
        bom: pd.DataFrame,
        *,
        include_print: bool = True,
    ) -> dict[str, Any]:
        return cls._machine_time_details_from_frames(
            item_code,
            bom,
            include_print=include_print,
        )["summary"]

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
    def _average_board_price(
        cls,
        boards: pd.DataFrame,
        board: pd.Series | None = None,
    ) -> tuple[float | None, str]:
        """Return a transparent fallback rate for an existing unpriced board."""

        if boards.empty or "price_per_tonne" not in boards:
            return None, ""
        candidates = boards.copy()
        candidates["_price"] = pd.to_numeric(
            candidates["price_per_tonne"], errors="coerce"
        )
        candidates = candidates[candidates["_price"].gt(0)]
        if candidates.empty:
            return None, ""

        source = "Average current board price"
        if board is not None:
            gsm = cls._positive_number(board.get("board_gsm")) or cls._positive_number(
                board.get("resolved_gsm")
            )
            if gsm:
                candidate_gsm = pd.Series(
                    float("nan"), index=candidates.index, dtype="float64"
                )
                if "board_gsm" in candidates:
                    candidate_gsm = pd.to_numeric(
                        candidates["board_gsm"], errors="coerce"
                    )
                if "resolved_gsm" in candidates:
                    candidate_gsm = candidate_gsm.fillna(
                        pd.to_numeric(candidates["resolved_gsm"], errors="coerce")
                    )
                same_gsm = candidates[candidate_gsm.eq(gsm)]
                if not same_gsm.empty:
                    candidates = same_gsm
                    source += f" ({gsm:,.0f} GSM)"
        return float(candidates["_price"].mean()), source

    @staticmethod
    def _is_plain_board(board: pd.Series | None) -> bool:
        """Return whether a stock row describes the unprinted board material."""

        if board is None:
            return False
        description = " ".join(
            str(board.get(field, "") or "")
            for field in ["board_item_name", "product_group", "product_state"]
        ).lower()
        return bool(board_material_spec(board.get("board_item_name", ""))) and not re.search(
            r"\bprint(?:ed|ing|er)?\b", description
        )

    @classmethod
    def _resolve_costing_board(
        cls,
        component_code: str,
        bom: pd.DataFrame,
        boards: pd.DataFrame,
        board_lookup: pd.DataFrame,
    ) -> tuple[pd.Series | None, str, pd.DataFrame]:
        """Resolve a printed-board BOM component to its underlying plain board."""

        def lookup(code: str) -> pd.Series | None:
            if board_lookup.empty or code not in board_lookup.index:
                return None
            row = board_lookup.loc[code]
            return row.iloc[0] if isinstance(row, pd.DataFrame) else row

        direct = lookup(component_code)
        if cls._is_plain_board(direct):
            return direct, component_code, pd.DataFrame()

        informational = pd.to_numeric(
            bom.get("is_informational_row", 0), errors="coerce"
        ).fillna(0)
        child_lines = bom[
            bom["bom_code"].astype(str).eq(str(component_code))
            & bom["cost_type"].astype(str).eq("Material")
            & informational.eq(0)
        ].copy()
        child_board_lines = child_lines[
            child_lines["cost_code"].astype(str).str.upper().str.startswith("BRD")
        ]
        for _, child_line in child_board_lines.iterrows():
            child_code = str(child_line.get("cost_code", ""))
            child = lookup(child_code)
            if cls._is_plain_board(child):
                return child, child_code, child_lines

        # Some printed stock rows carry the same article number as the plain
        # board even when their child-BOM link is incomplete.
        if direct is not None and not boards.empty:
            article = str(direct.get("resolved_article_no", "") or "").strip().rstrip("/")
            if article:
                same_article = boards[
                    boards.get("resolved_article_no", "")
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.rstrip("/")
                    .eq(article)
                ]
                plain_matches = same_article[
                    same_article.apply(cls._is_plain_board, axis=1)
                ]
                if not plain_matches.empty:
                    plain_matches = plain_matches.copy()
                    plain_matches["_priced"] = pd.to_numeric(
                        plain_matches.get("price_per_tonne"), errors="coerce"
                    ).fillna(0).gt(0)
                    plain_matches = plain_matches.sort_values(
                        ["_priced", "board_item_code"], ascending=[False, True]
                    )
                    row = plain_matches.iloc[0]
                    return row, str(row.get("board_item_code", "")), child_lines

        return direct, component_code, child_lines

    @classmethod
    def _printed_board_usage_factor(
        cls,
        parent_line: pd.Series,
        child_lines: pd.DataFrame,
        costing_board: pd.Series | None,
        costing_board_code: str,
    ) -> float:
        """Return child-BOM batches required per 1,000 finished products."""

        parent_quantity = cls._positive_number(parent_line.get("quantity")) or 0.0
        if parent_quantity <= 0:
            return 0.0
        if "tonne" not in str(parent_line.get("unit_of_measure", "")).lower():
            return parent_quantity
        if child_lines.empty:
            return 0.0
        child_board = child_lines[
            child_lines["cost_code"].astype(str).eq(str(costing_board_code))
        ]
        if child_board.empty:
            return 0.0
        tonnes_per_child_batch = cls._board_tonnes_per_1000(
            child_board.iloc[0], costing_board
        )
        if not tonnes_per_child_batch:
            return 0.0
        return parent_quantity / tonnes_per_child_batch

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
                    "board_material_spec": "",
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
        board_materials: list[str] = []
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

            board, costing_component_code, child_lines = cls._resolve_costing_board(
                component_code, bom, boards, board_lookup
            )
            resolved_plain_board = costing_component_code != component_code
            tonnes = cls._board_tonnes_per_1000(line, board)
            rate = cls._positive_number(board.get("price_per_tonne")) if board is not None else None
            source = str(board.get("price_source", "")) if board is not None else ""
            article = str(board.get("resolved_article_no", "")) if board is not None else ""
            period = str(board.get("price_period", "")) if board is not None else ""
            material_spec = (
                board_material_spec(board.get("board_item_name", ""))
                if board is not None
                else board_material_spec(line.get("cost_description", ""))
            )

            if rate is None and board is not None:
                rate, average_source = cls._average_board_price(boards, board)
                if rate is not None:
                    source = average_source
                    period = "Current supplied board-price list"

            if resolved_plain_board:
                resolution_source = f"Plain board from printed BOM component {component_code}"
                source = f"{resolution_source}; {source}" if source else resolution_source

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
            if costing_component_code not in board_items:
                board_items.append(costing_component_code)
            if material_spec and material_spec not in board_materials:
                board_materials.append(material_spec)
            if article and article != "nan" and article not in articles:
                articles.append(article)
            if source and source not in sources:
                sources.append(source)
            if period and period != "nan" and period not in periods:
                periods.append(period)
            detail_rows.append(
                {
                    "component_type": "Board",
                    "component_code": costing_component_code,
                    "description": (
                        board.get("board_item_name", "")
                        if board is not None
                        else line.get("cost_description", "")
                    ),
                    "quantity": line.get("quantity", 0),
                    "unit_of_measure": line.get("unit_of_measure", ""),
                    "rate": rate,
                    "tonnes_per_1000": tonnes,
                    "cost_per_1000": cost,
                    "article_no": article,
                    "material_spec": material_spec,
                    "printed_component_code": component_code if resolved_plain_board else "",
                    "source": source,
                }
            )

            if resolved_plain_board and not child_lines.empty:
                usage_factor = cls._printed_board_usage_factor(
                    line, child_lines, board, costing_component_code
                )
                child_materials = child_lines[
                    ~child_lines["cost_code"]
                    .astype(str)
                    .str.upper()
                    .str.startswith("BRD")
                ]
                for _, child_line in child_materials.iterrows():
                    child_quantity = pd.to_numeric(
                        child_line.get("quantity"), errors="coerce"
                    )
                    child_extended = pd.to_numeric(
                        child_line.get("extended_cost"), errors="coerce"
                    )
                    standard_quantity = (
                        float(child_quantity) if pd.notna(child_quantity) else 0.0
                    )
                    standard_cost = (
                        float(child_extended) if pd.notna(child_extended) else 0.0
                    )
                    scaled_cost = standard_cost * usage_factor
                    other_total += scaled_cost
                    detail_rows.append(
                        {
                            "component_type": "Print-route component",
                            "component_code": str(child_line.get("cost_code", "")),
                            "description": child_line.get("cost_description", ""),
                            "quantity": standard_quantity * usage_factor,
                            "unit_of_measure": child_line.get("unit_of_measure", ""),
                            "rate": child_line.get("unit_cost", 0),
                            "cost_per_1000": scaled_cost,
                            "standard_quantity_per_1000_boards": standard_quantity,
                            "standard_cost_per_1000_boards": standard_cost,
                            "quantity_basis": "Per 1,000 printed board sheets",
                            "printed_component_code": component_code,
                            "source": (
                                f"Standard BOM component from {component_code} "
                                f"× {usage_factor:.6g}"
                            ),
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
            "board_material_spec": " | ".join(board_materials),
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

    def machine_time_summary(
        self,
        item_code: str,
        *,
        include_print: bool = True,
    ) -> dict[str, Any]:
        return self._machine_time_from_frames(
            item_code,
            self.load_bom_lines(),
            include_print=include_print,
        )

    def machine_time_breakdown(self, item_code: str) -> dict[str, Any]:
        """Return the auditable operation lines behind machine hours."""
        return self._machine_time_details_from_frames(item_code, self.load_bom_lines())

    def _calculate_material_summary(self) -> pd.DataFrame:
        bom = self.load_bom_lines()
        boards = self.load_board_items()
        if bom.empty:
            return pd.DataFrame(columns=["item_code", *COST_INPUT_COLUMNS])
        summaries = []
        for item_code in bom["bom_code"].dropna().astype(str).unique():
            result = self._material_breakdown_from_frames(item_code, bom, boards)
            summaries.append({"item_code": item_code, **result["summary"]})
        return pd.DataFrame(summaries)

    def rebuild_material_summary(self) -> pd.DataFrame:
        """Recalculate the material feed after a BOM or board-price import."""
        summary = self._calculate_material_summary()
        self._atomic_csv_write(summary, self.material_summary_path)
        return summary

    def load_material_summary(self) -> pd.DataFrame:
        if self.material_summary_path.exists():
            return self._read_reference_csv(self.material_summary_path)
        signature = (
            self._file_signature(self.bom_path),
            self._file_signature(self.board_items_path),
        )
        cached = self._derived_frames.get("material_summary")
        if cached is None or cached[0] != signature:
            cached = (signature, self._calculate_material_summary())
            self._derived_frames["material_summary"] = cached
        return cached[1].copy()

    def load_board_catalog(self) -> pd.DataFrame:
        """Load dimensioned boards, including rows that still need a price."""

        boards = self.load_board_items().copy()
        if boards.empty:
            return boards
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
        boards["material_spec"] = boards["board_item_name"].map(
            board_material_spec
        )
        boards["is_plain_board"] = boards.apply(self._is_plain_board, axis=1)
        return boards[width.gt(0) & length.gt(0) & gsm.gt(0)].copy()

    def load_priced_board_catalog(self) -> pd.DataFrame:
        boards = self.load_board_catalog()
        if boards.empty:
            return boards
        price = pd.to_numeric(boards["price_per_tonne"], errors="coerce")
        return boards[boards["is_plain_board"].eq(True) & price.gt(0)].copy()

    def fitting_boards(
        self,
        *,
        net_length_mm: float,
        net_width_mm: float,
        board_gsm: float = 0.0,
        manufacturing_site: str = "",
        limit: int = 12,
    ) -> pd.DataFrame:
        """Return efficient boards ranked by maximum complete-net x-up."""

        boards = self.load_board_catalog().copy()
        if boards.empty:
            return boards
        boards = boards[boards["is_plain_board"].eq(True)].copy()
        if boards.empty:
            return boards
        boards["fit_units"] = boards.apply(
            lambda row: board_fit_units(
                net_length_mm,
                net_width_mm,
                row["effective_length_mm"],
                row["effective_width_mm"],
            ),
            axis=1,
        )
        boards = boards[boards["fit_units"].gt(0)].copy()
        if boards.empty:
            return boards

        if board_gsm > 0:
            boards = boards[
                pd.to_numeric(boards["effective_gsm"], errors="coerce").eq(
                    float(board_gsm)
                )
            ]
            if boards.empty:
                return boards
        site = str(manufacturing_site or "").strip().split(".")[0]
        if site:
            same_site = boards[
                boards["board_item_code"].astype(str).str.contains(
                    f"/{site}/", regex=False
                )
            ]
            if not same_site.empty:
                boards = same_site

        piece_area = float(net_length_mm) * float(net_width_mm)
        board_area = (
            pd.to_numeric(boards["effective_length_mm"], errors="coerce")
            * pd.to_numeric(boards["effective_width_mm"], errors="coerce")
        )
        boards["unused_area_mm2"] = (
            board_area - (piece_area * boards["fit_units"])
        ).clip(lower=0)
        boards["has_price"] = pd.to_numeric(
            boards["price_per_tonne"], errors="coerce"
        ).gt(0)
        return (
            boards.sort_values(
                ["fit_units", "unused_area_mm2", "has_price", "board_item_code"],
                ascending=[False, True, False, True],
            )
            .drop_duplicates("board_item_code")
            .head(max(1, int(limit)))
            .copy()
        )

    def new_item_material_breakdown(
        self,
        board_item_code: str,
        *,
        units_out: float,
        component_template_item_code: str = "",
        board_price_per_tonne: float | None = None,
        manual_board: dict[str, Any] | None = None,
        number_of_colours: int = 0,
    ) -> dict[str, Any]:
        if units_out <= 0:
            raise ValueError("The verified board fit must be at least 1-up.")
        if manual_board:
            board = pd.Series(
                {
                    "board_item_code": board_item_code,
                    "board_item_name": manual_board.get("board_item_name", ""),
                    "effective_width_mm": manual_board.get("board_width_mm", 0),
                    "effective_length_mm": manual_board.get("board_length_mm", 0),
                    "effective_gsm": manual_board.get("board_gsm", 0),
                    "resolved_article_no": manual_board.get("board_code", ""),
                    "price_per_tonne": board_price_per_tonne,
                    "price_period": "Entered for new board",
                    "price_source": "Entered for this new board",
                }
            )
        else:
            catalog = self.load_board_catalog()
            selected = catalog[
                catalog["board_item_code"].astype(str) == str(board_item_code)
            ]
            if selected.empty:
                raise ValueError("Choose a board item with usable dimensions and GSM.")
            board = selected.iloc[0].copy()

        width = self._positive_number(board.get("effective_width_mm"))
        length = self._positive_number(board.get("effective_length_mm"))
        gsm = self._positive_number(board.get("effective_gsm"))
        if not width or not length or not gsm:
            raise ValueError("Board width, length and GSM must all be greater than zero.")
        tonnes = (
            width
            * length
            * gsm
            / 1_000_000_000
            / units_out
        )
        rate = self._positive_number(board_price_per_tonne) or self._positive_number(
            board.get("price_per_tonne")
        )
        entered_board_price = bool(
            self._positive_number(board_price_per_tonne) is not None
            and (
                manual_board
                or self._positive_number(board.get("price_per_tonne")) is None
            )
        )
        if rate is None:
            raise ValueError(
                "This board has no price. Enter a board price per tonne to continue."
            )
        if (
            not manual_board
            and self._positive_number(board.get("price_per_tonne")) is None
            and self._positive_number(board_price_per_tonne) is not None
        ):
            board["price_period"] = "Entered for new costing"
            board["price_source"] = "Entered for this unpriced board"
        board_cost = tonnes * rate
        article_value = board.get("resolved_article_no", "")
        board_article = "" if pd.isna(article_value) else str(article_value or "")
        period_value = board.get("price_period", "")
        board_period = "" if pd.isna(period_value) else str(period_value or "")
        source_value = board.get("price_source", "")
        board_source = "" if pd.isna(source_value) else str(source_value or "")
        material_spec = board_material_spec(
            board.get("board_item_name", ""),
            manual_board.get("material_spec", "") if manual_board else "",
        )

        other_lines = pd.DataFrame()
        printed_routing_line = pd.DataFrame()
        include_print = int(number_of_colours or 0) > 0
        if component_template_item_code:
            template = self.material_breakdown(component_template_item_code)
            template_lines = template["lines"]
            if not template_lines.empty:
                component_types = ["Other component"]
                if include_print:
                    component_types.append("Print-route component")
                other_lines = template_lines[
                    template_lines["component_type"].isin(component_types)
                ].copy()
                print_route = other_lines[
                    other_lines["component_type"].eq("Print-route component")
                ].copy()
                if not print_route.empty:
                    print_route["quantity"] = pd.to_numeric(
                        print_route.get("standard_quantity_per_1000_boards", 0),
                        errors="coerce",
                    ).fillna(0) / units_out
                    print_route["cost_per_1000"] = pd.to_numeric(
                        print_route.get("standard_cost_per_1000_boards", 0),
                        errors="coerce",
                    ).fillna(0) / units_out
                    print_route["source"] = (
                        "Complete print-route BOM standard quantity ÷ "
                        f"{units_out:g}-up"
                    )
                    other_lines.loc[print_route.index, print_route.columns] = print_route
                if include_print:
                    printed_components = (
                        template_lines["printed_component_code"].fillna("").astype(str)
                        if "printed_component_code" in template_lines
                        else pd.Series("", index=template_lines.index, dtype="object")
                    )
                    printed_rows = template_lines[
                        printed_components.str.strip().ne("")
                    ]
                    printed_code = (
                        str(printed_rows.iloc[0].get("printed_component_code", ""))
                        if not printed_rows.empty
                        else ""
                    )
                    printed_routing_line = pd.DataFrame(
                        [
                            {
                                "component_type": "Printed board routing",
                                "component_code": printed_code,
                                "description": "Printed board and print operation included",
                                "quantity": 0,
                                "unit_of_measure": "Information",
                                "rate": 0,
                                "tonnes_per_1000": 0,
                                "cost_per_1000": 0,
                                "source": "Comparable BOM print route",
                            }
                        ]
                    )
        other_total = (
            float(pd.to_numeric(other_lines.get("cost_per_1000", 0), errors="coerce").fillna(0).sum())
            if not other_lines.empty
            else 0.0
        )
        machine_time = (
            self.machine_time_summary(
                component_template_item_code,
                include_print=include_print,
            )
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
                    "article_no": board_article,
                    "material_spec": material_spec,
                    "source": board_source,
                }
            ]
        )
        summary = {
            "materials_cost_per_1000": round(board_cost + other_total, 4),
            "board_item_code": board_item_code,
            "board_article_code": board_article,
            "board_price_per_tonne": round(rate, 4),
            "board_price_period": board_period,
            "board_price_source": board_source,
            "board_material_spec": material_spec,
            "board_tonnes_per_1000": round(tonnes, 6),
            "board_cost_per_1000": round(board_cost, 4),
            "other_components_cost_per_1000": round(other_total, 4),
            "component_template_item_code": component_template_item_code,
            "units_out": units_out,
            "new_board_required": int(bool(manual_board)),
            "new_board_item_code": board_item_code if manual_board else "",
            "new_board_material_spec": (
                str(manual_board.get("material_spec", "")) if manual_board else ""
            ),
            "new_board_price_per_tonne": round(rate, 4) if entered_board_price else 0.0,
            "print_operations_included": int(include_print),
            "material_cost_source": (
                "Selected plain board plus complete standard-quantity BOM template"
            ),
            **machine_time,
        }
        return {
            "summary": summary,
            "lines": pd.concat(
                [board_line, printed_routing_line, other_lines],
                ignore_index=True,
                sort=False,
            ),
        }

    def load_current_items(self) -> pd.DataFrame:
        if not self.items_path.exists():
            return pd.DataFrame(columns=SPECIFICATION_COLUMNS)
        signature = self.reference_data_version()
        cached = self._derived_frames.get("current_items")
        if cached is not None and cached[0] == signature:
            return cached[1].copy()
        items = self._read_reference_csv(self.items_path)
        items = items.merge(self.load_bom_summary(), on="item_code", how="left")
        items = items.merge(self.load_material_summary(), on="item_code", how="left")
        defaults: dict[str, Any] = {
            "customer_name": "",
            "material": "",
            "net_length_mm": 0.0,
            "net_width_mm": 0.0,
            "fulfilment_type": "MTO",
            "quantity_input_mode": "Pallets",
            "order_quantity": 0,
            "order_pallets": 0,
            "agreement_term_months": 12,
            "delivery_pallets_per_calloff": 0,
            "estimated_delivery_count": 1,
            "pallet_holding_charge_per_pallet_per_week": 0.0,
            "delivery_postcode": "",
            "delivery_method": "Haulier",
            "transport_service": "Next Day",
            "transport_vendor_preference": "Highest available",
            "transport_vendor": "",
            "transport_booking": "AM/PM",
            "transport_rate_zone": "",
            "transport_manual_override": 0,
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

        # The material grade is embedded in the board description.  It is not a
        # selectable product category, and older summary CSVs do not contain it.
        raw_boards = self.load_board_items()
        bom = self.load_bom_lines()
        board_lookup = (
            raw_boards.set_index("board_item_code", drop=False)
            if not raw_boards.empty and "board_item_code" in raw_boards
            else pd.DataFrame()
        )
        resolved_components: dict[str, tuple[str, str]] = {}

        def resolve_component(code: str) -> tuple[str, str]:
            if code in resolved_components:
                return resolved_components[code]
            board, resolved_code, _ = self._resolve_costing_board(
                code, bom, raw_boards, board_lookup
            )
            material_value = (
                board_material_spec(board.get("board_item_name", ""))
                if board is not None
                else ""
            )
            resolved_components[code] = (resolved_code, material_value)
            return resolved_components[code]

        def board_values(value: Any) -> tuple[str, str]:
            codes: list[str] = []
            materials: list[str] = []
            for raw_code in str(value or "").split("|"):
                code = raw_code.strip()
                if not code:
                    continue
                resolved_code, material_value = resolve_component(code)
                if resolved_code and resolved_code not in codes:
                    codes.append(resolved_code)
                if material_value and material_value not in materials:
                    materials.append(material_value)
            return " | ".join(codes), " | ".join(materials)

        board_value_pairs = items["board_item_code"].map(board_values)
        items["board_item_code"] = board_value_pairs.map(lambda value: value[0])
        derived_material = board_value_pairs.map(lambda value: value[1])
        items["board_material_spec"] = derived_material
        items["material"] = derived_material.where(
            derived_material.astype(str).str.strip().ne(""), items["material"]
        )
        self._derived_frames["current_items"] = (signature, items)
        return items.copy()

    def _load_csv_history(self) -> pd.DataFrame:
        if not self.history_path.exists() or self.history_path.stat().st_size == 0:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        history = pd.read_csv(self.history_path)
        if "quote_items" in history:
            def parse_quote_items(value: Any) -> list[dict[str, Any]]:
                if isinstance(value, list):
                    return value
                text = str(value or "").strip()
                if not text or text.lower() == "nan":
                    return []
                try:
                    parsed = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    return []
                return parsed if isinstance(parsed, list) else []

            history["quote_items"] = history["quote_items"].map(parse_quote_items)
        for column in HISTORY_COLUMNS:
            if column not in history:
                history[column] = None
        username = history["created_by_username"].fillna("").astype(str).str.strip()
        history.loc[username.eq(""), "created_by_username"] = history.loc[
            username.eq(""), "created_by"
        ]
        return history[HISTORY_COLUMNS]

    @staticmethod
    def _history_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
        history = pd.DataFrame(records)
        if history.empty:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        missing = [column for column in HISTORY_COLUMNS if column not in history]
        if missing:
            history = pd.concat(
                [
                    history,
                    pd.DataFrame(
                        {column: [None] * len(history) for column in missing},
                        index=history.index,
                    ),
                ],
                axis=1,
            )
        username = history["created_by_username"].fillna("").astype(str).str.strip()
        history.loc[username.eq(""), "created_by_username"] = history.loc[
            username.eq(""), "created_by"
        ]
        return history[HISTORY_COLUMNS]

    def load_history(self) -> pd.DataFrame:
        if self.uses_database:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT record FROM public.costing_revisions "
                            "ORDER BY created_at_utc, revision"
                        )
                        records = [row["record"] for row in cursor.fetchall()]
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "The costing database could not be reached. Please try again."
                ) from exc
            return self._history_frame(records)
        return self._load_csv_history()

    def import_csv_history_to_database(self) -> tuple[int, int]:
        """Copy legacy CSV revisions into Neon without creating duplicates."""
        if not self.uses_database:
            return (0, 0)
        history = self._load_csv_history()
        if history.empty:
            return (0, 0)
        imported = 0
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    for _, row in history.iterrows():
                        record = self._json_ready(row.to_dict())
                        costing_id = str(record.get("costing_id") or "").strip()
                        item_code = str(record.get("item_code") or "").strip()
                        if not costing_id or not item_code:
                            continue
                        cursor.execute(
                            "INSERT INTO public.costing_revisions "
                            "(costing_id, item_code, revision, source_item_code, "
                            "customer_name, quote_reference, created_at_utc, "
                            "created_by_email, created_by_username, created_by_name, record) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING",
                            (
                                costing_id,
                                item_code,
                                int(float(record.get("revision") or 1)),
                                str(record.get("source_item_code") or ""),
                                str(record.get("customer_name") or ""),
                                str(record.get("quote_reference") or ""),
                                self._timestamp(
                                    record.get("created_at_utc"),
                                    fallback=datetime.now(timezone.utc),
                                ),
                                str(record.get("created_by") or "unknown"),
                                str(
                                    record.get("created_by_username")
                                    or record.get("created_by")
                                    or "unknown"
                                ),
                                str(record.get("created_by_name") or ""),
                                Jsonb(record),
                            ),
                        )
                        imported += max(0, cursor.rowcount)
        except (psycopg.Error, TypeError, ValueError) as exc:
            raise RepositoryBusyError(
                "The existing costing history could not be imported."
            ) from exc
        return imported, len(history)

    def load_sessions(self) -> pd.DataFrame:
        """Load the small runtime session register used by the admin screen."""
        if self.uses_database:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT session_id, username, name, email, "
                            "signed_in_at_utc, last_activity_utc, last_heartbeat_utc, "
                            "active_seconds, current_page, force_logout, ended_at_utc "
                            "FROM public.app_sessions ORDER BY signed_in_at_utc"
                        )
                        sessions = pd.DataFrame(cursor.fetchall())
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "The session database could not be reached. Please try again."
                ) from exc
            for column in SESSION_COLUMNS:
                if column not in sessions:
                    sessions[column] = ""
            return sessions[SESSION_COLUMNS]
        if not self.sessions_path.exists() or self.sessions_path.stat().st_size == 0:
            return pd.DataFrame(columns=SESSION_COLUMNS, dtype=object)
        sessions = pd.read_csv(self.sessions_path, dtype=str, keep_default_na=False)
        for column in SESSION_COLUMNS:
            if column not in sessions:
                sessions[column] = ""
        return sessions[SESSION_COLUMNS]

    def expire_inactive_sessions(self, timeout_minutes: int) -> int:
        """Close sessions whose last user activity is beyond the timeout."""
        timeout_minutes = max(5, int(timeout_minutes))
        timeout = timedelta(minutes=timeout_minutes)
        cutoff = datetime.now(timezone.utc) - timeout
        if self.uses_database:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE public.app_sessions SET ended_at_utc = "
                            "last_activity_utc + (%s * interval '1 minute') "
                            "WHERE ended_at_utc IS NULL AND last_activity_utc < %s",
                            (timeout_minutes, cutoff),
                        )
                        return max(0, int(cursor.rowcount))
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "Inactive sessions could not be closed. Please try again."
                ) from exc

        if not self.sessions_path.exists() or self.sessions_path.stat().st_size == 0:
            return 0
        lock = FileLock(str(self.sessions_path) + ".lock", timeout=10)
        try:
            with lock:
                sessions = self.load_sessions()
                last_activity = pd.to_datetime(
                    sessions["last_activity_utc"], utc=True, errors="coerce"
                )
                ended = sessions["ended_at_utc"].fillna("").astype(str).str.strip().ne("")
                expired = ~ended & last_activity.lt(cutoff)
                if not expired.any():
                    return 0
                expiry_times = last_activity.loc[expired] + timeout
                sessions.loc[expired, "ended_at_utc"] = expiry_times.map(
                    lambda value: value.isoformat(timespec="seconds")
                )
                self._atomic_csv_write(sessions, self.sessions_path)
                return int(expired.sum())
        except Timeout as exc:
            raise RepositoryBusyError(
                "The session register is busy. Please try again in a moment."
            ) from exc

    def touch_session(self, values: dict[str, Any]) -> bool:
        """Update a session and return whether an administrator requested logout."""
        session_id = str(values.get("session_id", "") or "").strip()
        if not session_id:
            return False
        if self.uses_database:
            now = datetime.now(timezone.utc)
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT session_id, username, name, email, signed_in_at_utc, "
                            "last_activity_utc, last_heartbeat_utc, active_seconds, "
                            "current_page, force_logout, ended_at_utc "
                            "FROM public.app_sessions WHERE session_id = %s",
                            (session_id,),
                        )
                        existing = cursor.fetchone() or {}
                        merged = {
                            column: values.get(column, existing.get(column))
                            for column in SESSION_COLUMNS
                        }
                        merged.update(
                            {
                                "session_id": session_id,
                                "username": str(merged.get("username") or "unknown"),
                                "name": str(merged.get("name") or ""),
                                "email": str(merged.get("email") or "unknown"),
                                "signed_in_at_utc": self._timestamp(
                                    merged.get("signed_in_at_utc"), fallback=now
                                ),
                                "last_activity_utc": self._timestamp(
                                    merged.get("last_activity_utc"), fallback=now
                                ),
                                "last_heartbeat_utc": self._timestamp(
                                    merged.get("last_heartbeat_utc"), fallback=now
                                ),
                                "active_seconds": float(
                                    merged.get("active_seconds") or 0
                                ),
                                "current_page": str(merged.get("current_page") or ""),
                                "force_logout": bool(merged.get("force_logout") or False),
                                "ended_at_utc": self._timestamp(
                                    merged.get("ended_at_utc")
                                ),
                            }
                        )
                        cursor.execute(
                            "INSERT INTO public.app_sessions "
                            "(session_id, username, name, email, signed_in_at_utc, "
                            "last_activity_utc, last_heartbeat_utc, active_seconds, "
                            "current_page, force_logout, ended_at_utc) "
                            "VALUES (%(session_id)s, %(username)s, %(name)s, %(email)s, "
                            "%(signed_in_at_utc)s, %(last_activity_utc)s, "
                            "%(last_heartbeat_utc)s, %(active_seconds)s, %(current_page)s, "
                            "%(force_logout)s, %(ended_at_utc)s) "
                            "ON CONFLICT (session_id) DO UPDATE SET "
                            "username = EXCLUDED.username, name = EXCLUDED.name, "
                            "email = EXCLUDED.email, signed_in_at_utc = EXCLUDED.signed_in_at_utc, "
                            "last_activity_utc = EXCLUDED.last_activity_utc, "
                            "last_heartbeat_utc = EXCLUDED.last_heartbeat_utc, "
                            "active_seconds = EXCLUDED.active_seconds, "
                            "current_page = EXCLUDED.current_page, "
                            "force_logout = EXCLUDED.force_logout, "
                            "ended_at_utc = EXCLUDED.ended_at_utc",
                            merged,
                        )
                return bool(merged["force_logout"])
            except (psycopg.Error, TypeError, ValueError) as exc:
                raise RepositoryBusyError(
                    "The session database could not be updated. Please try again."
                ) from exc
        lock = FileLock(str(self.sessions_path) + ".lock", timeout=10)
        try:
            with lock:
                sessions = self.load_sessions()
                matches = sessions["session_id"].fillna("").astype(str).eq(session_id)
                if matches.any():
                    index = sessions.index[matches][0]
                    for key, value in values.items():
                        if key in SESSION_COLUMNS:
                            sessions.loc[index, key] = value
                else:
                    row = {column: values.get(column, "") for column in SESSION_COLUMNS}
                    if sessions.empty:
                        sessions = pd.DataFrame([row], columns=SESSION_COLUMNS, dtype=object)
                    else:
                        sessions = pd.concat(
                            [sessions, pd.DataFrame([row])], ignore_index=True
                        )
                current = sessions.loc[
                    sessions["session_id"].fillna("").astype(str).eq(session_id)
                ].iloc[-1]
                flag = pd.to_numeric(current.get("force_logout", 0), errors="coerce")
                self._atomic_csv_write(sessions, self.sessions_path)
                return bool(float(flag)) if pd.notna(flag) else False
        except Timeout as exc:
            raise RepositoryBusyError(
                "The session register is busy. Please try again in a moment."
            ) from exc

    def session_forced_logout(self, session_id: str) -> bool:
        if self.uses_database:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT force_logout FROM public.app_sessions "
                            "WHERE session_id = %s",
                            (str(session_id),),
                        )
                        row = cursor.fetchone()
                return bool(row and row["force_logout"])
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "The session database could not be checked. Please try again."
                ) from exc
        sessions = self.load_sessions()
        matches = sessions[
            sessions["session_id"].fillna("").astype(str).eq(str(session_id))
        ]
        if matches.empty:
            return False
        flag = pd.to_numeric(matches.iloc[-1]["force_logout"], errors="coerce")
        return bool(float(flag)) if pd.notna(flag) else False

    def force_logout_session(self, session_id: str) -> None:
        self.touch_session({"session_id": session_id, "force_logout": 1})

    def end_session(self, session_id: str) -> None:
        if not str(session_id or "").strip():
            return
        self.touch_session(
            {
                "session_id": session_id,
                "ended_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )

    def load_user_history(self, user_email: str) -> pd.DataFrame:
        """Return only revisions created by the signed-in user."""
        if self.uses_database:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT record FROM public.costing_revisions "
                            "WHERE lower(created_by_email) = lower(%s) "
                            "ORDER BY created_at_utc, revision",
                            (str(user_email).strip(),),
                        )
                        records = [row["record"] for row in cursor.fetchall()]
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "Your costing history could not be loaded. Please try again."
                ) from exc
            return self._history_frame(records)
        history = self.load_history()
        if history.empty:
            return history
        owner = str(user_email).strip().casefold()
        created_by = history["created_by"].fillna("").astype(str).str.strip().str.casefold()
        return history.loc[created_by.eq(owner)].copy()

    @staticmethod
    def _without_obsolete_products(products: pd.DataFrame) -> pd.DataFrame:
        """Remove products explicitly marked obsolete in their display text."""
        if products.empty:
            return products.copy()
        obsolete = pd.Series(False, index=products.index)
        for column in ("item_name", "description"):
            if column in products:
                obsolete |= (
                    products[column]
                    .fillna("")
                    .astype(str)
                    .str.contains("OBSOLETE", case=False, regex=False)
                )
        return products.loc[~obsolete].copy()

    def load_catalog(self) -> pd.DataFrame:
        """Return master feed items plus products deliberately added to the catalogue.

        Ordinary quotation revisions must never replace a stock-list product.  Those
        records contain customer and order-specific fields which belong in history,
        not in the shared product selector.
        """
        feed = self.load_current_items().copy()
        feed_bom_values = (
            feed["bom_available"]
            if "bom_available" in feed
            else pd.Series(0, index=feed.index, dtype=float)
        )
        feed_bom_available = pd.to_numeric(
            feed_bom_values, errors="coerce"
        ).fillna(0)
        feed = feed.loc[feed_bom_available.gt(0)].copy()
        feed = self._without_obsolete_products(feed)
        feed["source_type"] = "Stock list"
        feed_item_codes = set(feed["item_code"].fillna("").astype(str))

        if self.uses_database:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT DISTINCT ON (item_code) record "
                            "FROM public.costing_revisions "
                            "WHERE lower(COALESCE(record->>'catalogue_product', 'false')) "
                            "IN ('true', '1', 'yes') "
                            "ORDER BY item_code, created_at_utc DESC, revision DESC"
                        )
                        records = [row["record"] for row in cursor.fetchall()]
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "Saved products could not be loaded. Please try again."
                ) from exc
            history = self._history_frame(records)
        else:
            history = self.load_history()
            if not history.empty:
                catalogue_flag = (
                    history["catalogue_product"]
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .isin({"true", "1", "yes"})
                )
                history = history.loc[catalogue_flag].copy()
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
        latest_bom_values = (
            latest["bom_available"]
            if "bom_available" in latest
            else pd.Series(0, index=latest.index, dtype=float)
        )
        latest_material_values = (
            latest["materials_cost_per_1000"]
            if "materials_cost_per_1000" in latest
            else pd.Series(0, index=latest.index, dtype=float)
        )
        latest_bom_available = pd.to_numeric(
            latest_bom_values, errors="coerce"
        ).fillna(0)
        latest_material_cost = pd.to_numeric(
            latest_material_values, errors="coerce"
        ).fillna(0)
        latest = latest.loc[
            latest_bom_available.gt(0) | latest_material_cost.gt(0)
        ].copy()
        latest = self._without_obsolete_products(latest)
        # The stock/BOM feed remains authoritative even if an old or malformed
        # saved record was incorrectly marked as a catalogue product.
        latest = latest.loc[~latest["item_code"].isin(feed_item_codes)].copy()
        latest["source_type"] = "Saved costing"
        return pd.concat([feed, latest], ignore_index=True, sort=False)

    def save_costing(
        self,
        record: dict[str, Any],
        *,
        user_username: str | None = None,
        user_email: str,
        user_name: str,
    ) -> dict[str, Any]:
        if self.uses_database:
            item_code = str(record.get("item_code", "")).strip()
            now = datetime.now(timezone.utc)
            costing_id = f"C-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        # Serialise revision allocation for this item while still
                        # allowing different products to save concurrently.
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (item_code,),
                        )
                        # Quote numbers are allocated under one short global lock.
                        # This keeps references unique when several users save at
                        # the same time, without requiring another database object.
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            ("solidus_quote_number",),
                        )
                        cursor.execute(
                            "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                            "FROM public.costing_revisions WHERE item_code = %s",
                            (item_code,),
                        )
                        revision = int(cursor.fetchone()["revision"])
                        requested_quote_number = _safe_integer(
                            record.get("quote_number"), 0
                        )
                        if requested_quote_number > 0:
                            quote_number = requested_quote_number
                            cursor.execute(
                                "SELECT COALESCE(MAX(CASE WHEN record->>'quote_revision' "
                                "~ '^[0-9]+$' THEN (record->>'quote_revision')::integer "
                                "ELSE 0 END), 0) + 1 AS quote_revision "
                                "FROM public.costing_revisions "
                                "WHERE record->>'quote_number' = %s",
                                (str(quote_number),),
                            )
                            quote_revision = int(cursor.fetchone()["quote_revision"])
                        else:
                            cursor.execute(
                                "SELECT GREATEST(COALESCE(MAX(CASE "
                                "WHEN record->>'quote_number' ~ '^[0-9]+$' "
                                "THEN (record->>'quote_number')::integer "
                                "WHEN quote_reference ~ '^[0-9]+-[0-9]+$' "
                                "THEN split_part(quote_reference, '-', 1)::integer "
                                "ELSE 0 END), 0), 999) + 1 AS quote_number "
                                "FROM public.costing_revisions"
                            )
                            quote_number = int(cursor.fetchone()["quote_number"])
                            quote_revision = 1
                        quote_reference = f"{quote_number}-{quote_revision}"
                        saved = {
                            **record,
                            "quote_reference": quote_reference,
                            "quote_number": quote_number,
                            "quote_revision": quote_revision,
                            "costing_id": costing_id,
                            "revision": revision,
                            "created_at_utc": now.isoformat(timespec="seconds"),
                            "created_by": user_email,
                            "created_by_username": user_username or user_email,
                            "created_by_name": user_name,
                        }
                        safe_record = self._json_ready(saved)
                        cursor.execute(
                            "INSERT INTO public.costing_revisions "
                            "(costing_id, item_code, revision, source_item_code, "
                            "customer_name, quote_reference, created_at_utc, "
                            "created_by_email, created_by_username, created_by_name, record) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                costing_id,
                                item_code,
                                revision,
                                str(saved.get("source_item_code", "") or ""),
                                str(saved.get("customer_name", "") or ""),
                                str(saved.get("quote_reference", "") or ""),
                                now,
                                user_email,
                                user_username or user_email,
                                user_name,
                                Jsonb(safe_record),
                            ),
                        )
                return saved
            except psycopg.Error as exc:
                raise RepositoryBusyError(
                    "The costing could not be saved to the database. Please try again."
                ) from exc
        lock = FileLock(str(self.history_path) + ".lock", timeout=30)
        try:
            with lock:
                history = self.load_history()
                item_code = str(record.get("item_code", "")).strip()
                revisions = pd.to_numeric(
                    history.loc[history["item_code"] == item_code, "revision"],
                    errors="coerce",
                )
                revision = int(revisions.max()) + 1 if not revisions.empty else 1
                requested_quote_number = _safe_integer(record.get("quote_number"), 0)
                references = history.get(
                    "quote_reference", pd.Series(dtype=str)
                ).fillna("").astype(str)
                quote_numbers = pd.to_numeric(
                    references.str.extract(r"^(\d+)-\d+$", expand=False),
                    errors="coerce",
                )
                stored_quote_numbers = pd.to_numeric(
                    history.get("quote_number", pd.Series(dtype=float)),
                    errors="coerce",
                )
                if requested_quote_number > 0:
                    quote_number = requested_quote_number
                    matching = pd.to_numeric(
                        history.loc[
                            stored_quote_numbers.eq(quote_number), "quote_revision"
                        ],
                        errors="coerce",
                    )
                    quote_revision = (
                        int(matching.max()) + 1
                        if not matching.empty and pd.notna(matching.max())
                        else 1
                    )
                else:
                    largest = pd.concat([quote_numbers, stored_quote_numbers]).max()
                    quote_number = max(999, int(largest) if pd.notna(largest) else 999) + 1
                    quote_revision = 1
                now = datetime.now(timezone.utc)
                saved = {
                    **record,
                    "quote_reference": f"{quote_number}-{quote_revision}",
                    "quote_number": quote_number,
                    "quote_revision": quote_revision,
                    "costing_id": f"C-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
                    "revision": revision,
                    "created_at_utc": now.isoformat(timespec="seconds"),
                    "created_by": user_email,
                    "created_by_username": user_username or user_email,
                    "created_by_name": user_name,
                }
                row = pd.DataFrame(
                    [{column: saved.get(column, "") for column in HISTORY_COLUMNS}]
                )
                updated = pd.concat([history, row], ignore_index=True)
                self._atomic_csv_write(updated, self.history_path)
        except Timeout as exc:
            raise RepositoryBusyError(
                "Another user is saving a costing. Please try again in a moment."
            ) from exc
        return saved

    def update_costing_esign(
        self,
        costing_id: str,
        values: dict[str, Any],
        *,
        owner_email: str,
    ) -> dict[str, Any]:
        """Attach the latest e-sign state to one immutable costing revision."""
        if not self.uses_database:
            raise RepositoryBusyError("Neon is required for e-signature tracking.")
        allowed = {
            "esign_request_id", "esign_status", "esign_is_complete",
            "esign_is_declined", "esign_signers", "esign_test_mode",
            "esign_approved_by_username", "esign_approved_by_name",
            "esign_approved_by_email", "esign_approved_at_utc",
            "esign_internal_signer_role",
        }
        update = {
            key: self._json_ready(value)
            for key, value in values.items()
            if key in allowed
        }
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE public.costing_revisions SET record = record || %s "
                        "WHERE costing_id = %s AND lower(created_by_email) = lower(%s) "
                        "RETURNING record",
                        (Jsonb(update), str(costing_id), str(owner_email).strip()),
                    )
                    row = cursor.fetchone()
            if not row:
                raise RepositoryBusyError(
                    "This saved revision could not be found for the signed-in user."
                )
            return dict(row["record"])
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The e-signature status could not be saved to Neon."
            ) from exc

    def submit_commercial_approval_request(
        self,
        *,
        approval_basis: str,
        requester_username: str,
        requester_name: str,
        requester_email: str,
        item_code: str,
        customer_name: str,
        request_reason: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Store a red-costing request for a separate administrator session."""
        if not self.uses_database:
            raise RepositoryBusyError("Neon is required for remote approvals.")
        request_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE public.commercial_approval_requests "
                        "SET status = 'cancelled', decided_at_utc = %s, "
                        "decision_reason = 'Replaced by a newer request' "
                        "WHERE lower(requester_username) = lower(%s) "
                        "AND status = 'pending'",
                        (now, requester_username),
                    )
                    cursor.execute(
                        "INSERT INTO public.commercial_approval_requests "
                        "(request_id, approval_basis, requester_username, requester_name, "
                        "requester_email, item_code, customer_name, request_reason, status, "
                        "requested_at_utc, snapshot) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s) "
                        "RETURNING *",
                        (
                            request_id,
                            approval_basis,
                            requester_username,
                            requester_name,
                            requester_email,
                            item_code,
                            customer_name,
                            request_reason,
                            now,
                            Jsonb(self._json_ready(snapshot)),
                        ),
                    )
                    row = cursor.fetchone()
            return dict(row or {})
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The approval request could not be sent. Please try again."
            ) from exc

    def latest_commercial_approval(
        self,
        requester_username: str,
        approval_basis: str,
    ) -> dict[str, Any] | None:
        if not self.uses_database:
            return None
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM public.commercial_approval_requests "
                        "WHERE lower(requester_username) = lower(%s) "
                        "AND approval_basis = %s ORDER BY requested_at_utc DESC LIMIT 1",
                        (requester_username, approval_basis),
                    )
                    row = cursor.fetchone()
            return dict(row) if row else None
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The approval status could not be checked. Please try again."
            ) from exc

    def load_commercial_approval_requests(self, status: str = "pending") -> pd.DataFrame:
        if not self.uses_database:
            return pd.DataFrame()
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM public.commercial_approval_requests "
                        "WHERE status = %s ORDER BY requested_at_utc ASC",
                        (status,),
                    )
                    rows = cursor.fetchall()
            return pd.DataFrame(rows)
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "Approval requests could not be loaded. Please try again."
            ) from exc

    def decide_commercial_approval_request(
        self,
        request_id: str,
        *,
        approved: bool,
        admin_username: str,
        admin_name: str,
        admin_email: str,
        decision_reason: str,
    ) -> dict[str, Any]:
        if not self.uses_database:
            raise RepositoryBusyError("Neon is required for remote approvals.")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE public.commercial_approval_requests SET status = %s, "
                        "decided_at_utc = %s, decided_by_username = %s, "
                        "decided_by_name = %s, decided_by_email = %s, "
                        "decision_reason = %s WHERE request_id = %s AND status = 'pending' "
                        "RETURNING *",
                        (
                            "approved" if approved else "rejected",
                            datetime.now(timezone.utc),
                            admin_username,
                            admin_name,
                            admin_email,
                            decision_reason,
                            request_id,
                        ),
                    )
                    row = cursor.fetchone()
            if not row:
                raise RepositoryBusyError(
                    "This request has already been decided or is no longer available."
                )
            return dict(row)
        except RepositoryBusyError:
            raise
        except psycopg.Error as exc:
            raise RepositoryBusyError(
                "The approval decision could not be saved. Please try again."
            ) from exc

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
