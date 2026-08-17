from __future__ import annotations

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
    "board_tonnes_per_1000",
    "board_cost_per_1000",
    "other_components_cost_per_1000",
    "component_template_item_code",
    "units_out",
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
        matches = matches.sort_values(
            ["_priced", "board_item_code"], ascending=[False, True]
        )
        row = matches.iloc[0]
        return {
            "board_item_code": str(row.get("board_item_code", "") or ""),
            "board_code": str(row.get("resolved_article_no", "") or target).rstrip("/"),
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
            "board_price_period": str(row.get("price_period", "") or ""),
            "board_price_source": str(row.get("price_source", "") or ""),
            "board_item_name": str(row.get("board_item_name", "") or ""),
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
    ) -> dict[str, Any]:
        return cls._machine_time_details_from_frames(item_code, bom)["summary"]

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
        signature = self.reference_data_version()
        cached = self._derived_frames.get("current_items")
        if cached is not None and cached[0] == signature:
            return cached[1].copy()
        items = self._read_reference_csv(self.items_path)
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
        self._derived_frames["current_items"] = (signature, items)
        return items.copy()

    def _load_csv_history(self) -> pd.DataFrame:
        if not self.history_path.exists() or self.history_path.stat().st_size == 0:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        history = pd.read_csv(self.history_path)
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
