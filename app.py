from __future__ import annotations

import html
import hashlib
import json
import math
import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st

from src.auth import (
    configured_users_for_import,
    make_password_hash,
    require_user,
    session_timeout_minutes,
    sign_out_button,
)
from src.calculations import (
    ANNUAL_VOLUME_ADJUSTMENTS,
    COMEX_FACTORS,
    DEFAULT_ANNUAL_VOLUME_BAND,
    DEFAULT_TOOLING_CHARGE,
    FOC_TOOLING_AMORTISATION_PER_1000,
    MIN_PALLET_HOLDING_CHARGE,
    TOOLING_AMORTISATION_PER_1000,
    annual_volume_band_for_units,
    calculate_cost,
    operational_spread_metrics,
    price_from_spread_percent,
    spread_percent_from_price,
    traffic_light_result,
    validate_details,
)
from src.exports import history_pdf, quote_pdf, sage_stock_import_csv
from src.esign import (
    DropboxSignClient,
    ESignError,
    Signer,
    append_commercial_signature_page,
    commercial_approval_recipient,
)
from src.multi_item import MULTI_DELIVERY_MODES, price_multi_item_transport
from src.product_matcher import (
    COATING_OPTIONS,
    PRODUCT_FORMS,
    rank_product_matches,
)
from src.repository import (
    CALCULATION_COLUMNS,
    COST_INPUT_COLUMNS,
    CsvRepository,
    RepositoryBusyError,
    SPECIFICATION_COLUMNS,
    board_fit_layout,
    board_fit_units,
    board_material_spec,
    data_directory,
    flat_net_dimensions,
)
from src.signatures import (
    SignatureImageError,
    normalise_signature_image,
    signature_sha256,
)
from src.transport import HaulierRateTable, TransportLookupError


PROJECT_ROOT = Path(__file__).resolve().parent
STAGES = ["Product", "Order", "Costs", "Price", "Quote"]
EUR_RATE_URL = "https://api.frankfurter.dev/v2/rate/GBP/EUR?providers=ECB"


def configured_database_url() -> str:
    """Read the private Neon connection string from Streamlit Secrets."""
    try:
        return str(dict(st.secrets.get("database", {})).get("url", "")).strip()
    except FileNotFoundError:
        return ""


def configured_esign() -> dict[str, Any]:
    """Read Dropbox Sign test settings without exposing their values."""
    try:
        return dict(st.secrets.get("esign", {}))
    except FileNotFoundError:
        return {}


@st.cache_data(ttl=3_600, show_spinner=False)
def live_eur_per_gbp() -> tuple[float, str]:
    """Return the latest available ECB GBP/EUR reference rate."""
    request = Request(
        EUR_RATE_URL,
        headers={"Accept": "application/json", "User-Agent": "SolidusCostingTool/1.0"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rate = float(payload["rate"])
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("The exchange-rate service returned an invalid rate.")
        return rate, str(payload.get("date", "")).strip()
    except (HTTPError, URLError, OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "The live GBP to EUR rate is temporarily unavailable. Use GBP or try again shortly."
        ) from exc


@st.cache_resource(show_spinner=False)
def cached_repository(
    data_dir: str,
    database_url: str,
    repository_code_version: str,
) -> CsvRepository:
    """Keep reference-data caches and database connections across reruns."""
    # This argument is deliberately part of Streamlit's cache key. It prevents
    # a hot deployment from retaining an instance of the previous class.
    del repository_code_version
    return CsvRepository(data_dir, database_url=database_url)


@st.cache_resource(show_spinner=False)
def cached_rate_table(path: str, file_version: tuple[int, int]) -> HaulierRateTable:
    """Reload haulier rates only when the source file changes."""
    return HaulierRateTable(path)


@st.cache_data(ttl=30, show_spinner=False)
def cached_product_catalog(
    _repository: CsvRepository,
    reference_version: tuple[tuple[int, int], ...],
) -> pd.DataFrame:
    """Avoid rebuilding the product selector after every keystroke."""
    return _repository.load_catalog()

st.set_page_config(
    page_title="Solidus Costing Tool",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root {
        --solidus-yellow: #fdd615;
        --solidus-gold: #dac20a;
        --solidus-mist: #f3f5f2;
        --solidus-grey: #d4dedd;
        --solidus-ink: #000000;
    }
    .block-container { padding-top: 4.75rem !important; padding-bottom: 3rem; max-width: 1180px; }
    h1, h2, h3 { color: var(--solidus-ink); letter-spacing: -0.025em; }
    [data-testid="stSidebar"] { border-right: 1px solid var(--solidus-grey); }
    [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }
    [data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--solidus-grey); border-radius: 16px; }
    div[data-testid="stMetric"] { background: #ffffff; border: 1px solid var(--solidus-grey);
        border-top: 4px solid var(--solidus-yellow); border-radius: 14px; padding: 12px 16px;
        box-shadow: 0 3px 12px rgba(0,0,0,.035); }
    div[data-testid="stMetricLabel"] p, div[data-testid="stMetricValue"],
    div[data-testid="stMetricValue"] > div { white-space: normal !important;
        overflow: visible !important; text-overflow: clip !important; overflow-wrap: anywhere; }
    div[data-testid="stForm"] { border-color: var(--solidus-grey); border-radius: 14px; }
    .status-card { padding: 1rem 1.1rem; border-radius: 12px; background: var(--solidus-mist);
        border-left: 5px solid var(--solidus-yellow); margin: .5rem 0 1rem; }
    .small-note { color: #4a5050; font-size: .9rem; }
    .brand-banner { display: flex; align-items: center; justify-content: space-between;
        flex-wrap: wrap; gap: 1rem; padding: 1.15rem 1.25rem; margin: 0 0 .8rem; background: linear-gradient(105deg, #fbfcfb 0%, var(--solidus-mist) 70%);
        border-radius: 16px; border: 2px solid #bcc8c7; border-right: 12px solid var(--solidus-yellow);
        box-sizing: border-box; overflow: visible;
        box-shadow: 0 5px 18px rgba(0,0,0,.04); }
    .brand-name { font-size: 2.2rem; line-height: 1.2; font-weight: 800; letter-spacing: -.06em; padding-top: .1rem; }
    .brand-tagline { font-size: .9rem; font-weight: 700; margin-top: .35rem; }
    .brand-tool { font-size: 1rem; font-weight: 700; background: var(--solidus-yellow);
        padding: .55rem .8rem; border-radius: 999px; white-space: normal; text-align: center; }
    .detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
        gap: .75rem; margin: .75rem 0; }
    .detail-card { min-width: 0; background: #ffffff; border: 1px solid var(--solidus-grey);
        border-top: 4px solid var(--solidus-yellow); border-radius: 14px; padding: 12px 16px;
        box-shadow: 0 3px 12px rgba(0,0,0,.035); }
    .detail-label { color: #4a5050; font-size: .86rem; line-height: 1.25;
        margin-bottom: .45rem; overflow-wrap: anywhere; }
    .detail-value { color: var(--solidus-ink); font-size: 1.45rem; line-height: 1.18;
        overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
    .access-pill { display: inline-block; font-size: .78rem; font-weight: 700;
        padding: .28rem .62rem; border-radius: 999px; background: var(--solidus-mist);
        border: 1px solid var(--solidus-grey); margin-bottom: .5rem; }
    .amber-alert { background: #fff1c7; border: 3px solid #d97706;
        border-left-width: 10px; border-radius: 14px; padding: 1rem 1.1rem;
        margin: .75rem 0 1rem; color: #4a2a00; }
    .amber-alert strong { display: block; font-size: 1.22rem; margin-bottom: .35rem; }
    .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
        background: var(--solidus-yellow); color: var(--solidus-ink); border-color: var(--solidus-gold);
        border-radius: 10px; font-weight: 700; }
    .stButton > button, .stDownloadButton > button { border-radius: 10px; }
    .stButton > button[kind="primary"]:hover { background: var(--solidus-gold); color: var(--solidus-ink); }
    div[data-testid="stProgressBar"] > div > div { background-color: var(--solidus-yellow); }
    @media (max-width: 700px) {
        .block-container { padding: 4.25rem 1rem 2rem !important; }
        .brand-name { font-size: 1.9rem; }
        .detail-grid { grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def clean_record(values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if not pd.api.types.is_scalar(value):
            cleaned[key] = value
        else:
            cleaned[key] = "" if pd.isna(value) else value
    return cleaned


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def format_uk_datetime(
    value: Any,
    *,
    include_time: bool = True,
    default: str = "—",
) -> str:
    """Format stored timestamps consistently without changing their database value."""
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return default
    parsed = parsed.tz_convert("Europe/London")
    return parsed.strftime("%d/%m/%Y %H:%M" if include_time else "%d/%m/%Y")


def format_esign_datetime(value: Any, *, default: str = "") -> str:
    """Format Dropbox Sign epoch timestamps in UK local time."""
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        parsed = pd.to_datetime(value, unit="s", utc=True, errors="coerce")
    else:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return default
    return parsed.tz_convert("Europe/London").strftime("%d/%m/%Y %H:%M")


def format_frame_dates(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Return a display/export copy with UK-formatted date columns."""
    formatted = frame.copy()
    for column in columns:
        if column in formatted.columns:
            formatted[column] = formatted[column].map(format_uk_datetime)
    return formatted


def printed_item_code_fields(item_code: Any) -> tuple[int, int, str, str] | None:
    """Locate customer and print segments without assuming a fixed code length."""
    parts = str(item_code or "").strip().upper().split("/")
    for print_index in range(1, len(parts) - 1):
        if re.fullmatch(r"\d{2}", parts[print_index]) and re.fullmatch(
            r"\d{3,4}G", parts[print_index + 1]
        ):
            customer_index = print_index - 1
            if re.fullmatch(r"[A-Z0-9]{3,10}", parts[customer_index]):
                return (
                    customer_index,
                    print_index,
                    parts[customer_index],
                    parts[print_index],
                )
    return None


def customer_specific_item_code(
    item_code: Any,
    customer_code: Any,
    print_number: Any,
) -> str:
    fields = printed_item_code_fields(item_code)
    if fields is None:
        return str(item_code or "").strip().upper()
    customer_index, print_index, _, _ = fields
    parts = str(item_code or "").strip().upper().split("/")
    parts[customer_index] = str(customer_code or "").strip().upper()
    parts[print_index] = str(print_number or "").strip()
    return "/".join(parts)


def _sign_out_local_session(repository: CsvRepository, message: str) -> None:
    repository.end_session(st.session_state.get("app_session_id", ""))
    st.session_state.clear()
    st.session_state["login_notice"] = message
    if bool(getattr(st.user, "is_logged_in", False)):
        st.logout()
    st.rerun()


def track_user_session(
    repository: CsvRepository,
    user: Any,
    current_page: str,
) -> str:
    """Record an approximate active-use total for this browser session."""
    now = _utc_now()
    new_session = "app_session_id" not in st.session_state
    session_id = st.session_state.setdefault("app_session_id", uuid.uuid4().hex)

    signed_in = st.session_state.setdefault("app_signed_in_at", now.isoformat())
    previous_text = st.session_state.get("app_last_activity_at")
    previous = pd.to_datetime(previous_text, utc=True, errors="coerce")
    active_seconds = float(st.session_state.get("app_active_seconds", 0) or 0)
    if pd.notna(previous):
        elapsed = max(0.0, float((now - previous).total_seconds()))
        if elapsed > session_timeout_minutes() * 60:
            _sign_out_local_session(
                repository,
                "You were signed out after a period of inactivity.",
            )
        # Long gaps are idle time. Short gaps normally represent continued use.
        if elapsed <= 300:
            active_seconds += elapsed
    st.session_state.app_last_activity_at = now.isoformat()
    st.session_state.app_active_seconds = active_seconds
    last_sync = pd.to_datetime(
        st.session_state.get("app_last_session_sync_at"),
        utc=True,
        errors="coerce",
    )
    page_changed = st.session_state.get("app_last_persisted_page") != current_page
    sync_due = new_session or page_changed or pd.isna(last_sync) or (
        now - last_sync
    ).total_seconds() >= 15
    if sync_due:
        forced = repository.touch_session(
            {
                "session_id": session_id,
                "username": user.username,
                "name": user.name,
                "email": user.email,
                "signed_in_at_utc": signed_in,
                "last_activity_utc": now.isoformat(),
                "last_heartbeat_utc": now.isoformat(),
                "active_seconds": round(active_seconds, 1),
                "current_page": current_page,
            }
        )
        if forced:
            _sign_out_local_session(repository, "An administrator signed you out.")
        st.session_state.app_last_session_sync_at = now.isoformat()
        st.session_state.app_last_heartbeat_sync_at = now.isoformat()
        st.session_state.app_last_persisted_page = current_page
    return session_id


@st.fragment(run_every="30s")
def session_heartbeat(repository: CsvRepository, session_id: str) -> None:
    """Keep the admin view current and apply forced logouts promptly."""
    try:
        now = _utc_now()
        last_activity = pd.to_datetime(
            st.session_state.get("app_last_activity_at"), utc=True, errors="coerce"
        )
        if pd.notna(last_activity) and (
            now - last_activity
        ).total_seconds() > session_timeout_minutes() * 60:
            _sign_out_local_session(
                repository,
                "You were signed out after a period of inactivity.",
            )
        last_sync = pd.to_datetime(
            st.session_state.get("app_last_heartbeat_sync_at"),
            utc=True,
            errors="coerce",
        )
        if pd.isna(last_sync) or (now - last_sync).total_seconds() >= 25:
            forced = repository.touch_session(
                {"session_id": session_id, "last_heartbeat_utc": now.isoformat()}
            )
            if forced:
                _sign_out_local_session(
                    repository, "An administrator signed you out."
                )
            st.session_state.app_last_heartbeat_sync_at = now.isoformat()
    except RepositoryBusyError:
        st.warning("The activity database is temporarily unavailable.")


def default_draft() -> dict[str, Any]:
    return {
        "customer_name": "",
        "item_code": "",
        "item_name": "",
        "description": "",
        "material": "",
        "product_group": "Finished goods",
        "manufacturing_site": "101",
        "net_mass_kg": 0.0,
        "board_gsm": 1_000.0,
        "length_mm": 0.0,
        "width_mm": 0.0,
        "height_mm": 0.0,
        "net_length_mm": 0.0,
        "net_width_mm": 0.0,
        "board_width_mm": 0.0,
        "board_length_mm": 0.0,
        "number_of_colours": 0,
        "fsc": "",
        "board_code": "",
        "pallet_size": "1000x1200",
        "pallet_quantity": 1_000,
        "fulfilment_type": "MTO",
        "quantity_input_mode": "Units",
        "order_quantity": 0,
        "order_pallets": 0,
        "agreement_term_months": 12,
        "delivery_pallets_per_calloff": 0,
        "estimated_delivery_count": 1,
        "pallet_holding_charge_per_pallet_per_week": 0.0,
        "annual_volume_band": DEFAULT_ANNUAL_VOLUME_BAND,
        "annual_volume_units": 0,
        "comex_consistent_payer": False,
        "comex_strategic_customer": False,
        "comex_over_credit_limit": False,
        "comex_poor_payment_history": False,
        "bom_available": 0,
        "materials_cost_per_1000": 0.0,
        "board_item_code": "",
        "board_article_code": "",
        "board_price_per_tonne": 0.0,
        "board_price_period": "",
        "board_price_source": "",
        "board_tonnes_per_1000": 0.0,
        "board_cost_per_1000": 0.0,
        "other_components_cost_per_1000": 0.0,
        "component_template_item_code": "",
        "units_out": 1.0,
        "new_board_required": 0,
        "new_board_item_code": "",
        "new_board_material_spec": "",
        "new_board_price_per_tonne": 0.0,
        "print_operations_included": 0,
        "material_cost_source": "",
        "machine_hours_per_1000": 0.0,
        "machine_time_source": "No BOM machine-time profile",
        "delivery_postcode": "",
        "delivered_to": "",
        "delivery_method": "Haulier",
        "incoterm": "DAP",
        "transport_service": "Next Day",
        "transport_vendor_preference": "Highest available",
        "transport_vendor": "",
        "transport_booking": "AM/PM",
        "transport_rate_zone": "",
        "transport_manual_override": 0,
        "transport_total": 0.0,
        "spread_percent": 30.0,
        "additional_charge_description": "Forme / Stereo",
        "additional_charge_amount": DEFAULT_TOOLING_CHARGE,
        "additional_charge_foc": False,
        "quote_currency": "GBP",
        "eur_per_gbp": 1.0,
        "eur_rate_date": "",
        "eur_rate_source": "",
        "source_item_code": "",
        "based_on_existing_new_product": False,
        "catalogue_product": False,
    }


def draft_number(key: str, fallback: float = 0.0) -> float:
    try:
        return float(st.session_state.draft.get(key, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback


def update_flat_net_from_finished_size(*, force: bool = False) -> None:
    """Keep the editable flat-net estimate aligned with finished dimensions."""

    if st.session_state.get("spec_net_dimensions_manual") and not force:
        return
    net_length, net_width = flat_net_dimensions(
        st.session_state.get("spec_finished_length", 0),
        st.session_state.get("spec_finished_width", 0),
        st.session_state.get("spec_finished_height", 0),
    )
    st.session_state.spec_net_length = net_length
    st.session_state.spec_net_width = net_width
    st.session_state.spec_net_dimensions_manual = False


def mark_flat_net_manual() -> None:
    st.session_state.spec_net_dimensions_manual = True


def sync_new_board_material() -> None:
    st.session_state.spec_board_material = str(
        st.session_state.get("spec_new_board_material_spec", "") or ""
    ).strip().upper()


def draft_flag(key: str) -> bool:
    value = st.session_state.draft.get(key, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def currency_symbol(currency: Any = "GBP") -> str:
    return "€" if str(currency or "GBP").upper() == "EUR" else "£"


def format_unit_price(value: Any, currency: Any = "GBP") -> str:
    """Show every per-item amount to the agreed five decimal places."""
    try:
        return f"{currency_symbol(currency)}{float(value):,.5f}"
    except (TypeError, ValueError):
        return "—"


def quote_exchange_factor(draft: dict[str, Any] | None = None) -> float:
    values = draft if draft is not None else st.session_state.get("draft", {})
    if str(values.get("quote_currency", "GBP") or "GBP").upper() != "EUR":
        return 1.0
    try:
        return max(0.0001, float(values.get("eur_per_gbp", 1.0) or 1.0))
    except (TypeError, ValueError):
        return 1.0


def format_machine_duration(hours: Any, *, include_seconds: bool = False) -> str:
    """Format decimal machine hours as an easy-to-read duration."""
    try:
        total_seconds = max(0, round(float(hours) * 3_600))
    except (TypeError, ValueError):
        total_seconds = 0
    if not include_seconds:
        total_minutes = round(total_seconds / 60)
        whole_hours, minutes = divmod(total_minutes, 60)
        return (
            f"{whole_hours:d} hr {minutes:d} min"
            if whole_hours
            else f"{minutes:d} min"
        )
    whole_hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if whole_hours:
        parts.append(f"{whole_hours:d} hr")
    parts.append(f"{minutes:d} min")
    parts.append(f"{seconds:d} sec")
    return " ".join(parts)


def show_detail_cards(
    items: list[tuple[str, Any]],
    *,
    container: Any = None,
) -> None:
    """Show product facts without Streamlit metric's one-line truncation."""
    cards = "".join(
        '<div class="detail-card">'
        f'<div class="detail-label">{html.escape(str(label))}</div>'
        f'<div class="detail-value">{html.escape(str(value))}</div>'
        "</div>"
        for label, value in items
    )
    target = container or st
    target.markdown(
        f'<div class="detail-grid">{cards}</div>', unsafe_allow_html=True
    )


def with_operational_spread(pricing: dict[str, float]) -> dict[str, float]:
    breakdown = st.session_state.get("breakdown", {})
    materials = float(
        breakdown.get(
            "material_base_per_1000",
            breakdown.get("materials_cost_per_1000", 0),
        )
        or 0
    )
    material_pricing = price_from_spread_percent(
        materials,
        pricing["spread_percent"],
    )
    metrics = operational_spread_metrics(
        material_pricing["spread_value_per_1000"],
        draft_number("order_quantity"),
        float(
            breakdown.get(
                "machine_hours_per_1000",
                draft_number("machine_hours_per_1000"),
            )
            or 0
        ),
    )
    return {
        **pricing,
        **metrics,
        "material_spread_value_per_1000": material_pricing[
            "spread_value_per_1000"
        ],
    }


def traffic_override_basis(pricing: dict[str, float]) -> str:
    """Fingerprint the figures an admin has explicitly approved."""
    payload = {
        "item_code": st.session_state.draft.get("item_code", ""),
        "order_quantity": draft_number("order_quantity"),
        "pricing_base_per_1000": float(
            st.session_state.breakdown.get("pricing_base_per_1000", 0) or 0
        ),
        "spread_percent": float(pricing.get("spread_percent", 0) or 0),
        "selling_price_per_1000": float(
            pricing.get("selling_price_per_1000", 0) or 0
        ),
        "quote_currency": st.session_state.draft.get("quote_currency", "GBP"),
        "eur_per_gbp": quote_exchange_factor(),
        "spread_per_machine_hour": float(
            pricing.get("spread_per_machine_hour", 0) or 0
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def reset_downstream() -> None:
    st.session_state.pop("breakdown", None)
    st.session_state.pop("pricing", None)
    st.session_state.pop("transport_quotes", None)
    st.session_state.pop("material_lines", None)
    st.session_state.pop("last_saved", None)
    st.session_state.pop("saved_revision_fingerprint", None)
    st.session_state.pop("pricing_base_for_inputs", None)
    st.session_state.pop("spread_percent_input", None)
    st.session_state.pop("selling_price_input", None)
    st.session_state.pop("fulfilment_type_input", None)
    st.session_state.pop("quantity_input_mode_input", None)
    st.session_state.pop("quote_reference", None)
    st.session_state.pop("quote_number", None)
    st.session_state.pop("quote_revision", None)
    st.session_state.pop("customer_contact", None)
    st.session_state.pop("customer_role", None)
    st.session_state.pop("customer_email", None)
    st.session_state.pop("approval_recipient_name", None)
    st.session_state.pop("approval_recipient_email", None)
    st.session_state.pop("approval_recipient_role", None)
    st.session_state.pop("approval_recipient_is_cover", None)
    st.session_state.pop("additional_charge_description", None)
    st.session_state.pop("additional_charge_amount", None)
    st.session_state.pop("additional_charge_foc", None)
    st.session_state.pop("quote_notes", None)
    st.session_state.pop("customer_item_customer_code", None)
    st.session_state.pop("customer_item_print_number", None)
    for key in list(st.session_state):
        if (
            key.startswith("spec_board_")
            or key.startswith("spec_finished_")
            or key.startswith("spec_net_")
            or key in {
            "spec_fsc",
            "spec_new_board",
            "spec_new_board_item_code",
            "spec_new_board_material_spec",
            "board_lookup_notice",
            "board_fit_suggestion",
            }
        ):
            st.session_state.pop(key, None)


def clear_costing_workflow() -> None:
    """Clear one browser's costing without signing the user out.

    A deliberate allow-list protects authentication, navigation and activity
    tracking. Everything else on the costing page is discarded so Streamlit
    widget values cannot leak into the next quotation.
    """
    protected_keys = {
        "authenticated_user",
        "login_notice",
        "main_navigation",
    }
    for key in list(st.session_state):
        if key in protected_keys or key.startswith("app_"):
            continue
        st.session_state.pop(key, None)
    st.session_state.step = 0
    st.session_state.workflow_notice = "The previous costing has been cleared."


def navigate_to(step: int) -> None:
    st.session_state.step = step
    st.rerun()


def can_access(step: int) -> bool:
    if step == 0:
        return True
    if not st.session_state.get("draft"):
        return False
    if step == 1:
        return True
    if validate_details(st.session_state.draft):
        return False
    if step == 2:
        return True
    if step == 3:
        return bool(st.session_state.get("breakdown"))
    pricing = st.session_state.get("pricing") or {}
    if not pricing:
        return False
    status = pricing.get("traffic_light_status")
    if status == "red":
        return True
    if status == "amber":
        return bool(pricing.get("traffic_amber_acknowledged"))
    return True


def stage_navigation(simple_mode: bool = False) -> None:
    if simple_mode:
        labels = ["Quote details", "Delivery", "Price & approval", "Save & send"]
        current = (
            0
            if st.session_state.step <= 1
            else st.session_state.step - 1
        )
        targets = [1 if st.session_state.get("draft") else 0, 2, 3, 4]
        columns = st.columns(4)
        for index, label in enumerate(labels):
            target = targets[index]
            if columns[index].button(
                label,
                key=f"simple_nav_{index}",
                width="stretch",
                disabled=not can_access(target),
                type="primary" if current == index else "secondary",
            ):
                navigate_to(target)
        st.progress(
            (current + 1) / len(labels),
            text=f"Step {current + 1} of {len(labels)}",
        )
        return

    columns = st.columns(5)
    for index, label in enumerate(STAGES):
        if columns[index].button(
            label,
            key=f"nav_{index}",
            width="stretch",
            disabled=not can_access(index),
            type="primary" if st.session_state.step == index else "secondary",
        ):
            navigate_to(index)
    progress = (st.session_state.step + 1) / len(STAGES)
    st.progress(progress, text=f"Step {st.session_state.step + 1} of {len(STAGES)}")


def show_cost_breakdown(breakdown: dict[str, float]) -> None:
    show_detail_cards(
        [
            ("Pricing base / 1,000", f"£{breakdown['pricing_base_per_1000']:,.2f}"),
            ("Pricing base / item", format_unit_price(breakdown["pricing_base_per_item"])),
            ("Pallets", f"{breakdown['pallet_count']:,.0f}"),
            ("Net kg / 1,000", f"{breakdown['net_weight_kg_per_1000']:,.2f}"),
        ]
    )
    rows = [("Calculated materials", breakdown["materials_cost_per_1000"])]
    adjustment = float(breakdown.get("material_adjustment_value_per_1000", 0) or 0)
    if adjustment:
        rows.extend(
            [
                ("Customer and volume adjustment", adjustment),
                ("Adjusted material base", breakdown["material_base_per_1000"]),
            ]
        )
    rows.append(("Delivery pass-through", breakdown["transport_cost_per_1000"]))
    table = pd.DataFrame(rows, columns=["Cost element", "Cost per 1,000"])
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "Cost per 1,000": st.column_config.NumberColumn(format="£%.2f")
        },
    )


def show_admin_adjustment_detail(breakdown: dict[str, float]) -> None:
    """Explain the hidden commercial adjustment to an admin only."""
    draft = st.session_state.draft
    annual_volume = draft_number("annual_volume_units")
    band = str(draft.get("annual_volume_band", DEFAULT_ANNUAL_VOLUME_BAND))
    volume_percent = float(
        breakdown.get(
            "annual_volume_adjustment_percent",
            ANNUAL_VOLUME_ADJUSTMENTS.get(band, 0),
        )
        or 0
    )
    selected_factors = [
        (label, percent)
        for key, (label, percent) in COMEX_FACTORS.items()
        if draft_flag(key)
    ]
    with st.expander("How the material adjustment was calculated"):
        st.write(
            f"Annual volume entered: {annual_volume:,.0f} units. Internal band: "
            f"{band}, applying {volume_percent:+.0f}%."
        )
        if selected_factors:
            factor_table = pd.DataFrame(
                selected_factors, columns=["Selected customer factor", "Adjustment"]
            )
            st.dataframe(
                factor_table,
                hide_index=True,
                width="stretch",
                column_config={
                    "Adjustment": st.column_config.NumberColumn(format="%+.0f%%")
                },
            )
        else:
            st.caption("No customer factors are selected.")
        st.write(
            f"Total material adjustment: "
            f"{float(breakdown.get('total_material_adjustment_percent', 0) or 0):+.0f}% "
            f"(£{float(breakdown.get('material_adjustment_value_per_1000', 0) or 0):,.2f} per 1,000)."
        )


def start_from_selected_product(
    selected: dict[str, Any],
    *,
    as_new_product: bool,
) -> None:
    st.session_state.pop("multi_item_mode", None)
    st.session_state.pop("multi_item_products", None)
    st.session_state.pop("multi_item_breakdowns", None)
    st.session_state.pop("multi_item_pricing", None)
    draft = default_draft()
    draft.update(
        {key: selected.get(key, draft.get(key)) for key in SPECIFICATION_COLUMNS}
    )
    draft.update(
        {key: selected.get(key, draft.get(key)) for key in COST_INPUT_COLUMNS}
    )
    if "spread_percent" in selected:
        draft["spread_percent"] = selected["spread_percent"]
    draft["source_item_code"] = selected.get("item_code", "")
    draft["based_on_existing_new_product"] = as_new_product
    if as_new_product:
        draft["bom_available"] = 0
        draft["component_template_item_code"] = selected.get("item_code", "")
        if not float(draft.get("net_length_mm", 0) or 0) or not float(
            draft.get("net_width_mm", 0) or 0
        ):
            net_length, net_width = flat_net_dimensions(
                draft.get("length_mm", 0),
                draft.get("width_mm", 0),
                draft.get("height_mm", 0),
            )
            draft["net_length_mm"] = net_length
            draft["net_width_mm"] = net_width
    st.session_state.draft = clean_record(draft)
    reset_downstream()
    navigate_to(1)


def render_select(
    repository: CsvRepository,
    can_create_new: bool,
    is_admin: bool,
) -> None:
    heading, access = st.columns([4, 1])
    heading.subheader("Choose a product")
    access.markdown(
        '<div class="access-pill">'
        + ("Can add new products" if can_create_new else "Existing products only")
        + "</div>",
        unsafe_allow_html=True,
    )
    st.caption("Search by product code or description.")

    routes = ["Existing product", "Multiple existing products"]
    if can_create_new:
        routes.append("New product")
    mode = st.radio("Costing route", routes, horizontal=True)

    if mode == "Existing product":
        catalog = cached_product_catalog(
            repository, repository.reference_data_version()
        )
        if catalog.empty:
            message = "There are no products in the stock list yet."
            if can_create_new:
                message += " You can add one using New product above."
            else:
                message += " Please ask someone with new-product access to add one."
            st.info(message)
            return
        catalog = catalog.sort_values("item_code").reset_index(drop=True)
        with st.expander("Help me find a product (beta)"):
            st.caption(
                "Use any details you know. Blank fields are ignored. This searches "
                "usable products with costing BOMs and puts the closest matches first."
            )
            st.caption(
                "Length and width are interchangeable, so enter them in whichever "
                "order is most natural for the product."
            )
            with st.form("product_finder_beta"):
                finder_type, finder_coating = st.columns(2)
                requested_form = finder_type.selectbox(
                    "Product type", PRODUCT_FORMS
                )
                requested_coating = finder_coating.selectbox(
                    "Coating", COATING_OPTIONS
                )
                finder_length, finder_width, finder_height, finder_gsm = st.columns(4)
                requested_length = finder_length.number_input(
                    "Length (mm)", min_value=0.0, step=1.0
                )
                requested_width = finder_width.number_input(
                    "Width (mm)", min_value=0.0, step=1.0
                )
                requested_height = finder_height.number_input(
                    "Height (mm)", min_value=0.0, step=1.0
                )
                requested_gsm = finder_gsm.number_input(
                    "GSM", min_value=0.0, step=50.0
                )
                find_matches = st.form_submit_button(
                    "Find closest matches", type="primary"
                )
            if find_matches:
                try:
                    matches = rank_product_matches(
                        catalog,
                        requested_form=requested_form,
                        requested_coating=requested_coating,
                        length_mm=requested_length,
                        width_mm=requested_width,
                        height_mm=requested_height,
                        gsm=requested_gsm,
                    )
                except ValueError as exc:
                    st.error(str(exc))
                    st.session_state.pop("product_match_results", None)
                else:
                    st.session_state.product_match_results = matches.to_dict(
                        orient="records"
                    )

            match_records = st.session_state.get("product_match_results", [])
            if match_records:
                matches = pd.DataFrame(match_records)

                def signed_difference(value: Any) -> str:
                    number = pd.to_numeric(value, errors="coerce")
                    if pd.isna(number):
                        return "—"
                    return f"{number:+,.0f}"

                def measurement(value: Any) -> str:
                    number = pd.to_numeric(value, errors="coerce")
                    return "—" if pd.isna(number) else f"{number:,.0f}"
                has_measurement = any(
                    value > 0
                    for value in (
                        requested_length,
                        requested_width,
                        requested_height,
                        requested_gsm,
                    )
                )

                def quality(distance: Any) -> str:
                    if not has_measurement:
                        return "🔵 Filter match"
                    number = float(distance or 0)
                    if number <= 0.05:
                        return "🟢 Very close"
                    if number <= 0.15:
                        return "🟡 Close"
                    return "🟠 Wider difference"

                suggestion_labels = {}
                for _, row in matches.iterrows():
                    code = str(row["item_code"])
                    size = (
                        f"{measurement(row['length_mm'])} × "
                        f"{measurement(row['width_mm'])} × "
                        f"{measurement(row['height_mm'])} mm"
                    )
                    differences = (
                        f"sides {signed_difference(row['difference_length_mm'])} / "
                        f"{signed_difference(row['difference_width_mm'])}, "
                        f"height {signed_difference(row['difference_height_mm'])} mm, "
                        f"GSM {signed_difference(row['difference_board_gsm'])}"
                    )
                    suggestion_labels[code] = (
                        f"{quality(row['match_distance'])} · {code} — "
                        f"{str(row.get('description', ''))[:75]} · {size} · "
                        f"{measurement(row['board_gsm'])} GSM · difference: {differences}"
                    )
                suggestion_codes = {
                    label: code for code, label in suggestion_labels.items()
                }
                selected_label = st.radio(
                    "Select a matching product",
                    list(suggestion_codes),
                    index=None,
                )
                suggested_code = suggestion_codes.get(selected_label)
                if st.button(
                    "Use selected product",
                    width="stretch",
                    disabled=suggested_code is None,
                ):
                    selected_rows = catalog.index[
                        catalog["item_code"].astype(str).eq(suggested_code)
                    ].tolist()
                    if selected_rows:
                        st.session_state.existing_product_index = selected_rows[0]
                        st.rerun()
            elif find_matches:
                st.warning(
                    "No usable products match those details. Try fewer filters."
                )
        labels = {
            index: (
                f"{row['item_code']} — "
                f"{str(row.get('description', ''))[:100]}"
            )
            for index, row in catalog.iterrows()
        }
        selected_index = st.selectbox(
            "Search existing products",
            options=list(labels),
            format_func=labels.get,
            index=None,
            placeholder="Search by item code or description",
            key="existing_product_index",
        )
        if selected_index is None:
            st.info("Search for the item you need above.")
            return
        selected = clean_record(catalog.loc[selected_index].to_dict())
        selected_material_total = float(
            selected.get("materials_cost_per_1000", 0) or 0
        )
        with st.container(border=True):
            st.markdown(f"### {selected.get('item_code', '')}")
            st.write(str(selected.get("description", "")))
            product_cards = [
                ("GSM", f"{float(selected.get('board_gsm', 0) or 0):,.0f}"),
                (
                    "Pallet quantity",
                    f"{float(selected.get('pallet_quantity', 0) or 0):,.0f}",
                ),
                (
                    "Size",
                    f"{float(selected.get('length_mm', 0) or 0):,.0f} × "
                    f"{float(selected.get('width_mm', 0) or 0):,.0f} × "
                    f"{float(selected.get('height_mm', 0) or 0):,.0f} mm",
                ),
            ]
            if is_admin:
                product_cards.insert(
                    2, ("Material / 1,000", f"£{selected_material_total:,.2f}")
                )
            show_detail_cards(product_cards)

        with st.expander("Product and material details" if is_admin else "Product details"):
            detail_cards = [
                ("Product group", selected.get("product_group", "—")),
                ("GSM", f"{float(selected.get('board_gsm', 0) or 0):,.0f}"),
                (
                    "Pallet quantity",
                    f"{float(selected.get('pallet_quantity', 0) or 0):,.0f}",
                ),
                ("From", selected.get("source_type", "Stock list")),
            ]
            if is_admin:
                detail_cards.insert(
                    3,
                    ("Calculated material / 1,000", f"£{selected_material_total:,.2f}"),
                )
            show_detail_cards(detail_cards)
            st.caption(
                f"{float(selected.get('length_mm', 0) or 0):,.0f} × "
                f"{float(selected.get('width_mm', 0) or 0):,.0f} × "
                f"{float(selected.get('height_mm', 0) or 0):,.0f} mm · "
                f"Net mass {float(selected.get('net_mass_kg', 0) or 0):,.4f} kg"
            )
            material_result = repository.material_breakdown(
                str(selected.get("item_code", ""))
            )
            material_lines = material_result["lines"]
            if is_admin and not material_lines.empty:
                st.caption(
                    f"Board price source: {selected.get('board_price_source', '—')}. "
                    "Machine and labour are excluded from every value shown here."
                )
                visible = [
                    "component_type",
                    "component_code",
                    "description",
                    "unit_of_measure",
                    "quantity",
                    "tonnes_per_1000",
                    "rate",
                    "cost_per_1000",
                    "source",
                ]
                st.dataframe(
                    material_lines[
                        [column for column in visible if column in material_lines]
                    ],
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "rate": st.column_config.NumberColumn(format="£%.2f"),
                        "cost_per_1000": st.column_config.NumberColumn(format="£%.2f"),
                        "tonnes_per_1000": st.column_config.NumberColumn(format="%.4f"),
                    },
                )
        has_material_cost = bool(
            float(selected.get("bom_available", 0) or 0)
            or selected_material_total > 0
        )
        if not has_material_cost:
            st.warning(
                "No costing BOM is available for this item, so it cannot be costed yet."
            )
        if st.button(
            "Start costing",
            type="primary",
            width="stretch",
            disabled=not has_material_cost,
        ):
            start_from_selected_product(selected, as_new_product=False)
    elif mode == "Multiple existing products":
        catalog = cached_product_catalog(
            repository, repository.reference_data_version()
        )
        if catalog.empty:
            st.info("There are no usable products in the stock list yet.")
            return
        catalog = catalog.sort_values("item_code").reset_index(drop=True)
        labels = {
            str(row["item_code"]): (
                f"{row['item_code']} — {str(row.get('description', ''))[:100]}"
            )
            for _, row in catalog.iterrows()
        }
        selected_codes = st.multiselect(
            "Add products to this quotation",
            options=list(labels),
            format_func=labels.get,
            placeholder="Search by item code or description",
        )
        st.caption(
            "Each item will have its own quantity, annual volume, selling price "
            "and traffic light. New products must still be costed one at a time."
        )
        if selected_codes:
            selected_preview = catalog.loc[
                catalog["item_code"].astype(str).isin(selected_codes),
                [
                    "item_code",
                    "description",
                    "length_mm",
                    "width_mm",
                    "height_mm",
                    "board_gsm",
                    "pallet_quantity",
                ],
            ].copy()
            st.dataframe(
                selected_preview,
                hide_index=True,
                width="stretch",
                column_config={
                    "item_code": st.column_config.TextColumn("Item"),
                    "description": st.column_config.TextColumn(
                        "Description", width="large"
                    ),
                    "length_mm": st.column_config.NumberColumn("Length", format="%.0f"),
                    "width_mm": st.column_config.NumberColumn("Width", format="%.0f"),
                    "height_mm": st.column_config.NumberColumn("Height", format="%.0f"),
                    "board_gsm": st.column_config.NumberColumn("GSM", format="%.0f"),
                    "pallet_quantity": st.column_config.NumberColumn(
                        "Per pallet", format="%.0f"
                    ),
                },
            )
        if st.button(
            "Start multi-item quotation",
            type="primary",
            width="stretch",
            disabled=len(selected_codes) < 2,
        ):
            products = [
                clean_record(
                    catalog.loc[
                        catalog["item_code"].astype(str).eq(code)
                    ].iloc[0].to_dict()
                )
                for code in selected_codes
            ]
            st.session_state.multi_item_mode = True
            st.session_state.multi_item_products = products
            st.session_state.multi_item_breakdowns = []
            st.session_state.multi_item_pricing = []
            st.session_state.step = 1
            reset_downstream()
            st.rerun()
    else:
        starting_point = st.radio(
            "How do you want to start?",
            ["Blank product", "Base on existing product"],
            horizontal=True,
        )
        if starting_point == "Blank product":
            with st.container(border=True):
                st.markdown("### Create a new product")
                st.write(
                    "Start with an empty specification and enter all product details."
                )
                if st.button("Create new product", type="primary", width="stretch"):
                    st.session_state.draft = default_draft()
                    reset_downstream()
                    navigate_to(1)
        else:
            catalog = cached_product_catalog(
                repository, repository.reference_data_version()
            )
            catalog = catalog.sort_values("item_code").reset_index(drop=True)
            labels = {
                index: (
                    f"{row['item_code']} — "
                    f"{str(row.get('description', ''))[:100]}"
                )
                for index, row in catalog.iterrows()
            }
            selected_index = st.selectbox(
                "Search for a product to use as the base",
                options=list(labels),
                format_func=labels.get,
                index=None,
                placeholder="Search by item code or description",
            )
            if selected_index is None:
                st.info("Choose the existing product you want to copy.")
                return
            selected = clean_record(catalog.loc[selected_index].to_dict())
            with st.container(border=True):
                st.markdown(f"### {selected.get('item_code', '')}")
                st.write(str(selected.get("description", "")))
                st.caption(
                    "Its specification and BOM will be used as the starting point. "
                    "You will enter a new item code on the next screen."
                )
            if st.button(
                "Base new product on this",
                type="primary",
                width="stretch",
            ):
                start_from_selected_product(selected, as_new_product=True)


def fill_board_details(repository: CsvRepository) -> None:
    code = str(st.session_state.get("spec_board_code", ""))
    match = repository.find_board_by_code(
        code,
        manufacturing_site=str(st.session_state.draft.get("manufacturing_site", "")),
    )
    if match is None:
        st.session_state.board_lookup_notice = (
            "error",
            f"No board details were found for {code.strip() or 'that code'}.",
        )
        return
    st.session_state.spec_board_code = match["board_code"]
    st.session_state.spec_board_gsm = float(match["board_gsm"])
    st.session_state.spec_board_width = float(match["board_width_mm"])
    st.session_state.spec_board_length = float(match["board_length_mm"])
    st.session_state.spec_fsc = match["fsc"]
    st.session_state.spec_board_price = float(match["board_price_per_tonne"])
    st.session_state.spec_board_material = str(
        match.get("board_material_spec", "") or ""
    )
    st.session_state.draft.update(match)
    st.session_state.draft["material"] = st.session_state.spec_board_material
    price = float(match["board_price_per_tonne"])
    price_text = f"£{price:,.2f}/tonne" if price > 0 else "no current price"
    st.session_state.board_lookup_notice = (
        "success" if price > 0 else "warning",
        f"{match['board_item_code']}: {match['board_width_mm']:,.0f} x "
        f"{match['board_length_mm']:,.0f} mm, {match['board_gsm']:,.0f} GSM, {price_text}.",
    )


def use_existing_board_suggestion(board: dict[str, Any]) -> None:
    """Apply a fit-checked board without losing the rest of the draft."""

    st.session_state.spec_new_board = False
    st.session_state.spec_board_code = str(
        board.get("resolved_article_no", "") or ""
    ).rstrip("/")
    st.session_state.spec_board_gsm = float(board.get("effective_gsm", 0) or 0)
    st.session_state.spec_board_width = float(
        board.get("effective_width_mm", 0) or 0
    )
    st.session_state.spec_board_length = float(
        board.get("effective_length_mm", 0) or 0
    )
    st.session_state.spec_fsc = str(board.get("fsc", "") or "")
    price_value = pd.to_numeric(board.get("price_per_tonne", 0), errors="coerce")
    st.session_state.spec_board_price = (
        float(price_value) if pd.notna(price_value) else 0.0
    )
    st.session_state.spec_board_material = str(
        board.get("material_spec", "")
        or board_material_spec(board.get("board_item_name", ""))
        or ""
    )
    st.session_state.draft.update(
        {
            "board_item_code": str(board.get("board_item_code", "") or ""),
            "board_code": st.session_state.spec_board_code,
            "board_gsm": st.session_state.spec_board_gsm,
            "board_width_mm": st.session_state.spec_board_width,
            "board_length_mm": st.session_state.spec_board_length,
            "fsc": st.session_state.spec_fsc,
            "material": st.session_state.spec_board_material,
            "board_material_spec": st.session_state.spec_board_material,
            "board_price_per_tonne": st.session_state.spec_board_price,
            "new_board_required": 0,
            "new_board_item_code": "",
            "new_board_material_spec": "",
            "new_board_price_per_tonne": 0.0,
        }
    )


def prepare_new_board_entry() -> None:
    """Clear an unsuitable board and open a clean new-board entry."""

    st.session_state.spec_new_board = True
    st.session_state.spec_board_code = ""
    st.session_state.spec_board_width = 0.0
    st.session_state.spec_board_length = 0.0
    st.session_state.spec_new_board_item_code = ""
    st.session_state.spec_new_board_material_spec = ""
    st.session_state.spec_board_material = ""
    st.session_state.spec_board_price = 0.0
    st.session_state.draft.update(
        {
            "board_item_code": "",
            "board_code": "",
            "board_width_mm": 0.0,
            "board_length_mm": 0.0,
            "new_board_required": 1,
            "new_board_item_code": "",
            "new_board_material_spec": "",
            "new_board_price_per_tonne": 0.0,
            "material": "",
            "board_material_spec": "",
            "board_price_per_tonne": 0.0,
        }
    )


def render_specification(
    repository: CsvRepository,
    simple_mode: bool = False,
) -> None:
    st.subheader("Order and fulfilment")
    draft = st.session_state.draft
    source_item_code = str(draft.get("source_item_code", "")).strip()
    based_on_existing = bool(draft.get("based_on_existing_new_product", False))
    existing_item = bool(source_item_code) and not based_on_existing
    board_widget_defaults = {
        "spec_board_code": str(draft.get("board_code", "")),
        "spec_board_gsm": draft_number("board_gsm"),
        "spec_board_width": draft_number("board_width_mm"),
        "spec_board_length": draft_number("board_length_mm"),
        "spec_fsc": str(draft.get("fsc", "")),
        "spec_board_price": draft_number(
            "new_board_price_per_tonne",
            draft_number("board_price_per_tonne"),
        ),
        "spec_board_material": str(
            draft.get("board_material_spec", "")
            or draft.get("material", "")
            or ""
        ),
        "spec_new_board": draft_flag("new_board_required"),
        "spec_new_board_item_code": str(draft.get("new_board_item_code", "")),
        "spec_new_board_material_spec": str(
            draft.get("new_board_material_spec", "")
        ),
    }
    for key, value in board_widget_defaults.items():
        st.session_state.setdefault(key, value)
    finished_widget_defaults = {
        "spec_finished_length": draft_number("length_mm"),
        "spec_finished_width": draft_number("width_mm"),
        "spec_finished_height": draft_number("height_mm"),
        "spec_net_length": draft_number("net_length_mm"),
        "spec_net_width": draft_number("net_width_mm"),
    }
    for key, value in finished_widget_defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("spec_net_dimensions_manual", False)
    new_board_required = bool(st.session_state.get("spec_new_board", False))
    new_board_item_code = str(
        st.session_state.get("spec_new_board_item_code", "") or ""
    )
    new_board_material_spec = str(
        st.session_state.get("spec_new_board_material_spec", "") or ""
    )
    board_price_input = float(
        st.session_state.get("spec_board_price", 0) or 0
    )
    board_fit_count = 0
    if existing_item:
        st.markdown(
            '<div class="status-card"><strong>'
            f"{str(draft.get('item_code', ''))}</strong> — "
            f"{str(draft.get('description', ''))}<br>"
            "Product details loaded. Open them below if anything needs changing.</div>",
            unsafe_allow_html=True,
        )
    elif based_on_existing:
        st.markdown(
            '<div class="status-card"><strong>New product based on '
            f"{html.escape(source_item_code)}</strong><br>"
            "Enter the new item code and change any product details required. "
            "The original product's BOM remains the costing source.</div>",
            unsafe_allow_html=True,
        )

    expander_label = (
        "View product specification"
        if simple_mode
        else "View or amend product specification"
        if existing_item
        else "Product specification *"
    )
    with st.expander(expander_label, expanded=not existing_item):
        left, right = st.columns(2)
        item_code = left.text_input(
            "Item code *", value=str(draft.get("item_code", "")), disabled=existing_item
        )
        description = right.text_input(
            "Description *",
            value=str(draft.get("description", "")),
            help="This description will be used on the saved costing and quotation.",
        )

        printed_fields = printed_item_code_fields(item_code)
        customer_code = ""
        print_number = ""
        if existing_item and printed_fields is not None:
            _, _, default_customer_code, default_print_number = printed_fields
            st.caption(
                "Customer-specific print details. These change the quotation item code; "
                "the costing still uses the selected product's BOM."
            )
            customer_col, print_col = st.columns(2)
            customer_code = customer_col.text_input(
                "Customer code *",
                value=default_customer_code,
                key="customer_item_customer_code",
                max_chars=10,
            )
            print_number = print_col.text_input(
                "Print number *",
                value=default_print_number,
                key="customer_item_print_number",
                max_chars=2,
                help="Use two digits, for example 02.",
            )

        col1, col2, col3 = st.columns(3)
        col1.text_input(
            "Board material",
            key="spec_board_material",
            disabled=True,
            help=(
                "Derived from the selected board description, for example "
                "KL/TKL.WPE. It is not a separate product-category choice."
            ),
        )
        material = str(st.session_state.get("spec_board_material", "") or "")
        product_group = col2.text_input(
            "Product group",
            value=str(draft.get("product_group", "Finished goods")),
            disabled=simple_mode,
        )
        board_gsm = col3.number_input(
            "Grade / GSM *",
            min_value=0.0,
            step=25.0,
            key="spec_board_gsm",
            disabled=simple_mode,
        )

        col1, col2, col3 = st.columns(3)
        length_mm = col1.number_input(
            "Length (mm) *",
            min_value=0.0,
            step=1.0,
            key="spec_finished_length",
            on_change=update_flat_net_from_finished_size,
            disabled=simple_mode,
        )
        width_mm = col2.number_input(
            "Width (mm) *",
            min_value=0.0,
            step=1.0,
            key="spec_finished_width",
            on_change=update_flat_net_from_finished_size,
            disabled=simple_mode,
        )
        height_mm = col3.number_input(
            "Height (mm) *",
            min_value=0.0,
            step=1.0,
            key="spec_finished_height",
            on_change=update_flat_net_from_finished_size,
            disabled=simple_mode,
        )

        if not existing_item:
            st.markdown("##### Complete flat net / blank")
            net_col1, net_col2, net_action = st.columns([1, 1, 0.8])
            net_length_mm = net_col1.number_input(
                "Flat net length (mm) *",
                min_value=0.0,
                step=1.0,
                key="spec_net_length",
                on_change=mark_flat_net_manual,
                help="Use the full CAD/forme bounding length, including every flap.",
            )
            net_width_mm = net_col2.number_input(
                "Flat net width (mm) *",
                min_value=0.0,
                step=1.0,
                key="spec_net_width",
                on_change=mark_flat_net_manual,
                help="Use the full CAD/forme bounding width, including every flap.",
            )
            net_action.button(
                "Recalculate net",
                on_click=update_flat_net_from_finished_size,
                kwargs={"force": True},
                width="stretch",
                help="Reset the estimate to finished length/width plus twice the height.",
            )
            st.caption(
                "Board fit uses this complete flat net, never the finished length and "
                "width. The starting estimate adds twice the height to both plan "
                "dimensions; replace it with the CAD/forme footprint where required."
            )
        else:
            net_length_mm = draft_number("net_length_mm")
            net_width_mm = draft_number("net_width_mm")

        col1, col2 = st.columns(2)
        pallet_quantity = col1.number_input(
            "Pallet quantity *",
            min_value=0,
            value=max(0, int(draft_number("pallet_quantity"))),
            step=1,
            disabled=simple_mode,
        )
        net_mass_kg = col2.number_input(
            "Net mass per item (kg)",
            min_value=0.0,
            value=draft_number("net_mass_kg"),
            step=0.0001,
            format="%.4f",
            disabled=simple_mode,
        )

        col1, col2, col3 = st.columns(3)
        board_width_mm = col1.number_input(
            "Board width / reel width (mm)",
            min_value=0.0,
            step=1.0,
            key="spec_board_width",
            disabled=simple_mode,
        )
        board_length_mm = col2.number_input(
            "Board length / chop (mm)",
            min_value=0.0,
            step=1.0,
            key="spec_board_length",
            disabled=simple_mode,
        )
        number_of_colours = col3.number_input(
            "Print colour code",
            min_value=0,
            value=max(0, int(draft_number("number_of_colours"))),
            step=1,
            help="The first digit is the number of colours. Code 901 prints as CMYK.",
            disabled=simple_mode,
        )

        col1, col2 = st.columns(2)
        board_code = col1.text_input(
            "Board code",
            key="spec_board_code",
            disabled=simple_mode,
            help="Mill or article code. A trailing slash is not needed.",
        )
        fsc = col2.text_input("FSC", key="spec_fsc", disabled=simple_mode)
        if not existing_item and not new_board_required:
            st.button(
                "Fill board details from code",
                on_click=fill_board_details,
                args=(repository,),
            )
            notice = st.session_state.get("board_lookup_notice")
            if notice:
                getattr(st, notice[0])(notice[1])

        if not existing_item:
            st.markdown("#### Board fit")
            fit_layout = board_fit_layout(
                net_length_mm,
                net_width_mm,
                board_length_mm,
                board_width_mm,
            )
            board_fit_count = fit_layout["units"]
            if net_length_mm > 0 and net_width_mm > 0 and board_fit_count >= 2:
                st.success(
                    f"{board_fit_count}-up fits on the current board sheet "
                    f"({fit_layout['across']} × {fit_layout['down']} layout) using "
                    f"the complete {net_length_mm:,.0f} × {net_width_mm:,.0f} mm "
                    "flat net, with 10 mm at the sheet edges and between nets."
                )
            elif net_length_mm > 0 and net_width_mm > 0 and board_fit_count == 1:
                st.warning(
                    "1-up fits on the current board using the complete flat net, "
                    "but 2-up or more is the efficiency goal. A more efficient "
                    "stock board is suggested below where one is available."
                )

            if (
                net_length_mm > 0
                and net_width_mm > 0
                and board_fit_count < 2
                and not new_board_required
            ):
                suitable = repository.fitting_boards(
                    net_length_mm=net_length_mm,
                    net_width_mm=net_width_mm,
                    board_gsm=board_gsm,
                    manufacturing_site=str(draft.get("manufacturing_site", "")),
                )
                current_board_item = str(draft.get("board_item_code", "") or "")
                suitable = suitable[
                    pd.to_numeric(suitable["fit_units"], errors="coerce").fillna(0)
                    .gt(board_fit_count)
                    & ~suitable["board_item_code"].astype(str).eq(current_board_item)
                ].copy()
                if not suitable.empty:
                    if board_fit_count == 0:
                        st.warning(
                            "The current board does not fit the complete flat net. "
                            "Choose a suitable stock board below or enter a new one."
                        )
                    candidate_labels: dict[int, str] = {}
                    for candidate_index, candidate in suitable.iterrows():
                        price_value = pd.to_numeric(
                            candidate.get("price_per_tonne", 0), errors="coerce"
                        )
                        price = (
                            float(price_value) if pd.notna(price_value) else 0.0
                        )
                        price_text = (
                            f"£{price:,.2f}/tonne"
                            if price > 0
                            else "price required"
                        )
                        candidate_labels[candidate_index] = (
                            f"{int(candidate['fit_units'])}-up · "
                            f"{candidate['board_item_code']} · "
                            f"{float(candidate['effective_width_mm']):,.0f} × "
                            f"{float(candidate['effective_length_mm']):,.0f} mm · "
                            f"{candidate.get('material_spec', '') or 'material not parsed'} · "
                            f"{price_text}"
                        )
                    candidate_index = st.selectbox(
                        "Suitable existing boards",
                        options=list(candidate_labels),
                        format_func=candidate_labels.get,
                    )
                    candidate = clean_record(suitable.loc[candidate_index].to_dict())
                    fit_columns = st.columns(2)
                    fit_columns[0].button(
                        "Use this board",
                        on_click=use_existing_board_suggestion,
                        args=(candidate,),
                        width="stretch",
                    )
                    fit_columns[1].button(
                        "Enter a new board instead",
                        on_click=prepare_new_board_entry,
                        width="stretch",
                    )
                else:
                    if board_fit_count == 0:
                        st.error(
                            "No existing board of the required GSM fits the complete "
                            "flat net with the 10 mm margins. Enter a new board."
                        )
                        st.button(
                            "Enter a new board",
                            on_click=prepare_new_board_entry,
                        )
                    else:
                        st.caption(
                            "No current stock board of this GSM improves on 1-up."
                        )

            new_board_required = bool(st.session_state.get("spec_new_board", False))
            if new_board_required:
                st.info(
                    "This board is not in the current stock list. Enter its Sage "
                    "stock code, board material and plain-board price here."
                )
                new_code_col, material_col, price_col = st.columns(3)
                new_board_item_code = new_code_col.text_input(
                    "New board Sage item code *",
                    key="spec_new_board_item_code",
                    placeholder="For example BRD001/101/NPL/1000G/WW",
                )
                new_board_material_spec = material_col.text_input(
                    "Board material specification *",
                    key="spec_new_board_material_spec",
                    placeholder="For example WT/BT",
                    on_change=sync_new_board_material,
                )
                board_price_input = price_col.number_input(
                    "Board price (£ per tonne) *",
                    min_value=0.0,
                    step=1.0,
                    key="spec_board_price",
                    help=(
                        "Enter the current plain-board price. It is saved with the "
                        "costing and used in the material calculation."
                    ),
                )
                st.caption(
                    "Use the plain-board stock code here. Printed routing is only "
                    "included when a print colour code is entered."
                )
                if board_fit_count == 0 and board_width_mm > 0 and board_length_mm > 0:
                    st.error(
                        "The new board dimensions still do not fit the complete flat "
                        "net with the 10 mm margins."
                    )
            else:
                board_catalog = repository.load_board_catalog()
                selected_board_rows = board_catalog[
                    board_catalog["board_item_code"].astype(str).eq(
                        str(draft.get("board_item_code", "") or "")
                    )
                ]
                catalog_price = 0.0
                if not selected_board_rows.empty:
                    price_value = pd.to_numeric(
                        selected_board_rows.iloc[0].get("price_per_tonne", 0),
                        errors="coerce",
                    )
                    catalog_price = (
                        float(price_value) if pd.notna(price_value) else 0.0
                    )
                if catalog_price <= 0:
                    board_price_input = st.number_input(
                        "Board price (£ per tonne) *",
                        min_value=0.0,
                        step=1.0,
                        key="spec_board_price",
                        help=(
                            "The selected stock board has no current price. Enter its "
                            "plain-board price here to continue."
                        ),
                    )
                else:
                    board_price_input = catalog_price
                    st.caption(f"Current plain-board price: £{catalog_price:,.2f} per tonne.")

    st.markdown("#### Required order details")
    customer_col, postcode_col = st.columns([1.4, 1.0])
    customer_name = customer_col.text_input(
        "Customer *", value=str(draft.get("customer_name", ""))
    )
    delivery_postcode = postcode_col.text_input(
        "Delivery postcode *", value=str(draft.get("delivery_postcode", ""))
    )

    fulfilment_options = ["MTO — Make to Order", "MTC — Make to Contract"]
    current_fulfilment = str(draft.get("fulfilment_type", "MTO") or "MTO").upper()
    st.session_state.setdefault(
        "fulfilment_type_input", fulfilment_options[1 if current_fulfilment == "MTC" else 0]
    )
    fulfilment_label = st.radio(
        "Fulfilment type",
        fulfilment_options,
        horizontal=True,
        key="fulfilment_type_input",
    )
    fulfilment_type = fulfilment_label[:3]

    quantity_modes = ["Units", "Pallets"]
    current_mode = str(draft.get("quantity_input_mode", "Units"))
    st.session_state.setdefault(
        "quantity_input_mode_input",
        current_mode if current_mode in quantity_modes else "Units",
    )
    quantity_input_mode = st.radio(
        "Enter order quantity as",
        quantity_modes,
        horizontal=True,
        key="quantity_input_mode_input",
    )
    safe_pallet_quantity = max(1, int(pallet_quantity))
    if quantity_input_mode == "Units":
        order_quantity = st.number_input(
            "Order quantity (units) *",
            min_value=0,
            value=max(0, int(draft_number("order_quantity"))),
            step=1_000,
        )
        order_pallets = (
            math.ceil(order_quantity / safe_pallet_quantity) if order_quantity else 0
        )
    else:
        default_pallets = int(draft_number("order_pallets"))
        if default_pallets <= 0 and draft_number("order_quantity") > 0:
            default_pallets = math.ceil(
                draft_number("order_quantity") / safe_pallet_quantity
            )
        order_pallets = st.number_input(
            "Order quantity (pallets) *",
            min_value=0,
            value=max(0, default_pallets),
            step=1,
        )
        order_quantity = int(order_pallets) * safe_pallet_quantity

    quantity_metrics = st.columns(3)
    quantity_metrics[0].metric("Units", f"{int(order_quantity):,}")
    quantity_metrics[1].metric("Pallets", f"{int(order_pallets):,}")
    quantity_metrics[2].metric("Units per pallet", f"{safe_pallet_quantity:,}")

    large_order_confirmed = True
    if int(order_pallets) > 26:
        st.warning(
            f"This order is {int(order_pallets):,} pallets. Are you sure? "
            "Please check that an extra zero has not been entered."
        )
        large_order_confirmed = st.checkbox(
            f"Yes, I confirm the order quantity is {int(order_pallets):,} pallets.",
            key=f"confirm_large_order_{int(order_pallets)}",
        )

    agreement_term_months = int(draft_number("agreement_term_months", 12))
    pallet_holding_charge = draft_number(
        "pallet_holding_charge_per_pallet_per_week"
    )
    if fulfilment_type == "MTC":
        st.markdown("#### Contract and call-off plan")
        col1, col2 = st.columns(2)
        agreement_term_months = col1.number_input(
            "Agreement term (months) *",
            min_value=1,
            value=max(1, agreement_term_months),
            step=1,
        )
        max_calloff = max(1, int(order_pallets))
        default_calloff = int(draft_number("delivery_pallets_per_calloff"))
        if default_calloff <= 0:
            default_calloff = min(10, max_calloff)
        delivery_pallets_per_calloff = col2.number_input(
            "Minimum pallets per delivery *",
            min_value=1,
            max_value=max_calloff,
            value=min(default_calloff, max_calloff),
            step=1,
            help=(
                "Transport will be costed using this as the minimum delivery size. "
                "Larger deliveries may be used where they reduce the number of call-offs."
            ),
        )
        pallet_holding_charge = st.number_input(
            "Potential holding charge (£ per pallet per week)",
            min_value=MIN_PALLET_HOLDING_CHARGE,
            value=max(MIN_PALLET_HOLDING_CHARGE, pallet_holding_charge),
            step=0.25,
            help=(
                f"The minimum storage rate is £{MIN_PALLET_HOLDING_CHARGE:.2f} "
                "per pallet per week. A higher rate can be entered."
            ),
        )
    else:
        delivery_pallets_per_calloff = max(1, int(order_pallets))

    estimated_delivery_count = (
        max(1, int(order_pallets) // int(delivery_pallets_per_calloff))
        if order_pallets
        else 0
    )
    if fulfilment_type == "MTC":
        st.caption(
            f"Planned profile: approximately {estimated_delivery_count:,} deliveries "
            f"with a minimum of {int(delivery_pallets_per_calloff):,} pallets per delivery."
        )
        st.caption(
            "The agreement term starts from the confirmed commencement date, "
            "based on current lead times and production planning. It does not "
            "start on the quotation date."
        )

    st.markdown("#### Customer and annual volume")
    annual_volume_units = st.number_input(
        "Expected annual volume (units) *",
        min_value=0,
        value=max(0, int(draft_number("annual_volume_units"))),
        step=1_000,
        help="Enter the customer's expected total unit volume over 12 months.",
    )
    positive, negative = st.columns(2)
    with positive:
        st.caption("Positive customer factors")
        consistent_payer = st.checkbox(
            COMEX_FACTORS["comex_consistent_payer"][0],
            value=draft_flag("comex_consistent_payer"),
        )
        strategic_customer = st.checkbox(
            COMEX_FACTORS["comex_strategic_customer"][0],
            value=draft_flag("comex_strategic_customer"),
        )
    with negative:
        st.caption("Negative customer factors")
        over_credit_limit = st.checkbox(
            COMEX_FACTORS["comex_over_credit_limit"][0],
            value=draft_flag("comex_over_credit_limit"),
        )
        poor_payment_history = st.checkbox(
            COMEX_FACTORS["comex_poor_payment_history"][0],
            value=draft_flag("comex_poor_payment_history"),
        )
    st.caption("Customer factors are used in the internal commercial calculation.")

    submitted = st.button(
        "Continue to delivery" if simple_mode else "Save order details",
        type="primary",
        disabled=not large_order_confirmed,
        width="stretch" if simple_mode else "content",
    )

    if submitted:
        if printed_fields is not None:
            if not re.fullmatch(r"[A-Z0-9]{3,10}", customer_code.strip().upper()):
                st.error("Customer code must contain 3 to 10 letters or numbers.")
                return
            if not re.fullmatch(r"\d{2}", print_number.strip()):
                st.error("Print number must contain two digits, for example 02.")
                return
            item_code = customer_specific_item_code(
                item_code, customer_code, print_number
            )
        if not existing_item:
            board_fit_count = board_fit_units(
                net_length_mm,
                net_width_mm,
                board_length_mm,
                board_width_mm,
            )
            if board_fit_count == 0:
                st.error(
                    "Enter complete flat-net and board dimensions that achieve at "
                    "least 1-up with the 10 mm margins."
                )
                return
            if new_board_required and not new_board_item_code.strip():
                st.error("Enter the new board's Sage item code.")
                return
            if new_board_required and not new_board_material_spec.strip():
                st.error("Enter the new board's material specification, for example WT/BT.")
                return
            if board_price_input <= 0:
                st.error("Enter the plain-board price per tonne.")
                return
        resolved_board_item_code = str(draft.get("board_item_code", "") or "")
        resolved_material = (
            new_board_material_spec.strip().upper()
            if new_board_required
            else material.strip()
        )
        resolved_board_price = board_price_input
        if not existing_item and not new_board_required and board_code.strip():
            resolved_board = repository.find_board_by_code(
                board_code,
                manufacturing_site=str(draft.get("manufacturing_site", "")),
            )
            if resolved_board is not None:
                resolved_board_item_code = str(
                    resolved_board.get("board_item_code", "") or ""
                )
                resolved_material = str(
                    resolved_board.get("board_material_spec", "")
                    or resolved_board.get("material", "")
                    or resolved_material
                )
                resolved_board_price = float(
                    resolved_board.get("board_price_per_tonne", 0)
                    or resolved_board_price
                )
        updated = {
            **draft,
            "customer_name": customer_name.strip(),
            "item_code": item_code.strip().upper(),
            "description": description.strip(),
            "material": resolved_material,
            "board_material_spec": resolved_material,
            "product_group": product_group.strip(),
            "board_gsm": board_gsm,
            "length_mm": length_mm,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "net_length_mm": net_length_mm,
            "net_width_mm": net_width_mm,
            "pallet_quantity": pallet_quantity,
            "order_quantity": order_quantity,
            "net_mass_kg": net_mass_kg,
            "board_width_mm": board_width_mm,
            "board_length_mm": board_length_mm,
            "number_of_colours": number_of_colours,
            "board_code": board_code.strip(),
            "fsc": fsc.strip(),
            "board_item_code": (
                new_board_item_code.strip().upper()
                if new_board_required
                else resolved_board_item_code
            ),
            "units_out": float(board_fit_count or draft_number("units_out", 1)),
            "new_board_required": int(new_board_required),
            "new_board_item_code": (
                new_board_item_code.strip().upper() if new_board_required else ""
            ),
            "new_board_material_spec": (
                new_board_material_spec.strip().upper() if new_board_required else ""
            ),
            "new_board_price_per_tonne": (
                float(board_price_input) if not existing_item else 0.0
            ),
            "board_price_per_tonne": float(resolved_board_price or board_price_input),
            "print_operations_included": int(int(number_of_colours) > 0),
            "fulfilment_type": fulfilment_type,
            "quantity_input_mode": quantity_input_mode,
            "order_pallets": int(order_pallets),
            "agreement_term_months": int(agreement_term_months),
            "delivery_pallets_per_calloff": int(delivery_pallets_per_calloff),
            "estimated_delivery_count": int(estimated_delivery_count),
            "pallet_holding_charge_per_pallet_per_week": float(
                pallet_holding_charge
            ),
            "annual_volume_units": int(annual_volume_units),
            "annual_volume_band": annual_volume_band_for_units(
                annual_volume_units
            ),
            "comex_consistent_payer": bool(consistent_payer),
            "comex_strategic_customer": bool(strategic_customer),
            "comex_over_credit_limit": bool(over_credit_limit),
            "comex_poor_payment_history": bool(poor_payment_history),
            "delivery_postcode": delivery_postcode.strip().upper(),
            "delivered_to": customer_name.strip(),
        }
        errors = validate_details(updated)
        if errors:
            for error in errors:
                st.error(error)
        else:
            st.session_state.draft = updated
            reset_downstream()
            st.success("Order details complete.")
            navigate_to(2)


def render_costs(
    repository: CsvRepository,
    rate_table: HaulierRateTable,
    is_admin: bool,
    simple_mode: bool = False,
) -> None:
    st.subheader("Delivery details" if simple_mode else "Material base and delivery")
    draft = st.session_state.draft
    source_item_code = str(draft.get("source_item_code") or draft.get("item_code", ""))
    material_result: dict[str, Any] | None = None
    selected_board: pd.Series | None = None

    if float(draft.get("bom_available", 0) or 0) and source_item_code:
        material_result = repository.material_breakdown(source_item_code)
        imported_total = float(
            material_result["summary"].get("materials_cost_per_1000", 0) or 0
        )
        if is_admin:
            st.success(
                f"Material cost from the BOM and board prices: £{imported_total:,.2f} per 1,000."
            )
        elif not simple_mode:
            st.success("The BOM and board-price calculation is ready.")
    else:
        st.info(
            "Confirm the board and choose the complete comparable BOM. The board "
            "is recalculated; every other standard-quantity BOM component is retained."
        )
        new_board_required = draft_flag("new_board_required")
        manual_board: dict[str, Any] | None = None
        selected_board_code = ""
        board_price_override = 0.0
        board_catalog = repository.load_board_catalog().copy()
        if "is_plain_board" in board_catalog:
            board_catalog = board_catalog[
                board_catalog["is_plain_board"].eq(True)
            ].copy()

        if new_board_required:
            selected_board_code = str(draft.get("new_board_item_code", "") or "")
            board_price_override = draft_number("new_board_price_per_tonne")
            material_spec = str(draft.get("new_board_material_spec", "") or "")
            manual_board = {
                "board_width_mm": draft_number("board_width_mm"),
                "board_length_mm": draft_number("board_length_mm"),
                "board_gsm": draft_number("board_gsm"),
                "board_code": str(draft.get("board_code", "") or ""),
                "material_spec": material_spec,
                "board_item_name": (
                    f"BOARD{draft_number('board_width_mm'):,.0f}X"
                    f"{draft_number('board_length_mm'):,.0f}/"
                    f"{draft_number('board_gsm'):,.0f}GSM/{material_spec}"
                ).replace(",", ""),
            }
            selected_board = pd.Series(
                {
                    "board_item_code": selected_board_code,
                    "effective_gsm": draft_number("board_gsm"),
                    "effective_width_mm": draft_number("board_width_mm"),
                    "effective_length_mm": draft_number("board_length_mm"),
                    "resolved_article_no": str(draft.get("board_code", "") or ""),
                    "price_per_tonne": board_price_override,
                }
            )
            show_detail_cards(
                [
                    ("New board", selected_board_code or "Code required"),
                    (
                        "Sheet",
                        f"{draft_number('board_width_mm'):,.0f} × "
                        f"{draft_number('board_length_mm'):,.0f} mm",
                    ),
                    ("GSM", f"{draft_number('board_gsm'):,.0f}"),
                    ("Material", material_spec or "Required"),
                    ("Board rate", f"£{board_price_override:,.2f} / tonne"),
                ]
            )
        else:
            required_gsm = draft_number("board_gsm")
            if required_gsm > 0:
                matching_gsm = board_catalog[
                    pd.to_numeric(
                        board_catalog["effective_gsm"], errors="coerce"
                    ).eq(required_gsm)
                ]
                if not matching_gsm.empty:
                    board_catalog = matching_gsm
            site = str(draft.get("manufacturing_site", "")).split(".")[0]
            if site:
                matching_site = board_catalog[
                    board_catalog["board_item_code"].astype(str).str.contains(
                        f"/{site}/", regex=False
                    )
                ]
                if not matching_site.empty:
                    board_catalog = matching_site
            board_catalog = board_catalog.sort_values(
                ["effective_gsm", "board_item_code"]
            ).drop_duplicates("board_item_code")
            board_labels = {}
            for _, row in board_catalog.iterrows():
                price_value = pd.to_numeric(
                    row.get("price_per_tonne", 0), errors="coerce"
                )
                price = float(price_value) if pd.notna(price_value) else 0.0
                price_text = (
                    f" — £{price:,.0f}/tonne"
                    if is_admin and price > 0
                    else " — price required"
                    if price <= 0
                    else ""
                )
                board_labels[str(row["board_item_code"])] = (
                    f"{row['board_item_code']} — {row.get('board_item_name', '')}"
                    f"{price_text}"
                )
            board_options = ["", *board_labels]
            current_board = str(draft.get("board_item_code", ""))
            selected_board_code = st.selectbox(
                "Board item *",
                board_options,
                index=board_options.index(current_board)
                if current_board in board_options
                else 0,
                format_func=lambda value: board_labels.get(
                    value, "Choose a board item"
                ),
            )
            if selected_board_code:
                selected_board = board_catalog[
                    board_catalog["board_item_code"]
                    .astype(str)
                    .eq(selected_board_code)
                ].iloc[0]
                board_price_value = pd.to_numeric(
                    selected_board.get("price_per_tonne", 0), errors="coerce"
                )
                board_price = (
                    float(board_price_value) if pd.notna(board_price_value) else 0.0
                )
                if board_price <= 0:
                    board_price_override = draft_number(
                        "new_board_price_per_tonne"
                    )
                    if board_price_override <= 0:
                        st.error(
                            "This board still has no price. Return to Product details "
                            "and enter its plain-board price."
                        )

        maximum_fit = board_fit_units(
            draft_number("net_length_mm"),
            draft_number("net_width_mm"),
            float(selected_board.get("effective_length_mm", 0) or 0)
            if selected_board is not None
            else draft_number("board_length_mm"),
            float(selected_board.get("effective_width_mm", 0) or 0)
            if selected_board is not None
            else draft_number("board_width_mm"),
        )
        if maximum_fit:
            units_out = float(maximum_fit)
            message = (
                f"Costing uses {maximum_fit}-up, the best verified fit for the "
                "complete flat net on this board with 10 mm margins."
            )
            if maximum_fit >= 2:
                st.success(message)
            else:
                st.warning(
                    message
                    + " 2-up or more remains the efficiency goal; return to Product "
                    "details to choose a better board where available."
                )
        else:
            units_out = 0.0
            st.error(
                "0-up: this board does not fit the complete flat net. Return to "
                "Product details and choose or enter a suitable board."
            )

        templates = repository.load_current_items()
        templates = templates[
            pd.to_numeric(templates["bom_available"], errors="coerce").fillna(0).gt(0)
        ].sort_values("item_code")
        template_labels = {
            str(row["item_code"]): f"{row['item_code']} — {row.get('description', '')}"
            for _, row in templates.iterrows()
        }
        template_options = ["", *template_labels]
        current_template = str(draft.get("component_template_item_code", ""))
        selected_template = st.selectbox(
            "Complete BOM template *",
            template_options,
            index=template_options.index(current_template)
            if current_template in template_options
            else 0,
            format_func=lambda value: template_labels.get(
                value, "Choose a comparable BOM"
            ),
            help=(
                "Copies every non-board material component at its BOM standard "
                "quantity, including banding, pallets, layercards, wrap and adhesive. "
                "The selected plain board is the only material component replaced."
            ),
        )
        if selected_board_code and selected_template and units_out > 0:
            template_code = selected_template
            try:
                material_result = repository.new_item_material_breakdown(
                    selected_board_code,
                    units_out=units_out,
                    component_template_item_code=template_code,
                    board_price_per_tonne=board_price_override or None,
                    manual_board=manual_board,
                    number_of_colours=int(draft_number("number_of_colours")),
                )
            except ValueError as exc:
                st.warning(str(exc))

    if not simple_mode:
        st.markdown("#### Material setup")
        st.caption("Board and other components are calculated from the product data.")
    if material_result is not None:
        material_summary = material_result["summary"]
        material_lines = material_result["lines"]
        if not float(draft.get("bom_available", 0) or 0):
            if int(draft_number("number_of_colours")) > 0:
                st.caption(
                    "The plain board is used for material cost. The comparable "
                    "BOM's printed-board route and print machine time are included "
                    "because a print colour code has been entered."
                )
            else:
                st.caption(
                    "The plain board is used for material cost. Printed-board and "
                    "print operations are left out because no print colour code was entered."
                )
        if is_admin:
            show_detail_cards(
                [
                    ("Board / 1,000", f"£{float(material_summary['board_cost_per_1000']):,.2f}"),
                    (
                        "Other components / 1,000",
                        f"£{float(material_summary['other_components_cost_per_1000']):,.2f}",
                    ),
                    (
                        "Total materials / 1,000",
                        f"£{float(material_summary['materials_cost_per_1000']):,.2f}",
                    ),
                    (
                        "Board rate",
                        f"£{float(material_summary['board_price_per_tonne']):,.2f} / tonne",
                    ),
                ]
            )
            st.caption(
                f"{material_summary.get('board_article_code') or material_summary.get('board_item_code', 'Board')} · "
                f"{material_summary.get('board_price_source', '')}"
            )
        elif not simple_mode:
            st.success("Material details are complete.")
        if is_admin and not material_lines.empty:
            with st.expander("View board and component calculation"):
                visible = [
                    "component_type",
                    "component_code",
                    "description",
                    "quantity",
                    "unit_of_measure",
                    "tonnes_per_1000",
                    "rate",
                    "cost_per_1000",
                    "source",
                ]
                st.dataframe(
                    material_lines[
                        [column for column in visible if column in material_lines]
                    ],
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "rate": st.column_config.NumberColumn(format="£%.2f"),
                        "cost_per_1000": st.column_config.NumberColumn(format="£%.2f"),
                        "tonnes_per_1000": st.column_config.NumberColumn(format="%.4f"),
                    },
                )
    else:
        material_summary = None
        st.warning("Choose both a board item and an other-component option to continue.")

    st.markdown("#### Transport")
    estimated_pallets = int(draft_number("order_pallets")) or math.ceil(
        draft_number("order_quantity") / draft_number("pallet_quantity", 1)
    )
    fulfilment_type = str(draft.get("fulfilment_type", "MTO") or "MTO").upper()
    planned_pallets_per_delivery = (
        max(1, int(draft_number("delivery_pallets_per_calloff")))
        if fulfilment_type == "MTC"
        else max(1, estimated_pallets)
    )
    estimated_deliveries = max(
        1, estimated_pallets // planned_pallets_per_delivery
    )
    if fulfilment_type == "MTC":
        st.caption(
            f"MTC agreement: {estimated_pallets:,} pallets across approximately "
            f"{estimated_deliveries:,} call-offs with a minimum of "
            f"{planned_pallets_per_delivery:,} pallets per delivery. "
            "Transport is priced across the full schedule."
        )
    else:
        st.caption(
            f"MTO order: {estimated_pallets:,} pallet(s) released as one delivery event. "
            "Rates cover 1–26 pallets per vehicle load; larger movements are split into additional loads."
        )
    current_method = str(draft.get("delivery_method", "Haulier"))
    collected = st.checkbox(
        "Collected",
        value=(
            current_method == "Customer collection"
            or str(draft.get("incoterm", "DAP") or "DAP").upper() == "EXW"
        ),
        help="Tick when the customer will collect. Otherwise the quotation is DAP and delivery is costed below.",
    )
    delivery_method = "Customer collection" if collected else "Haulier"
    incoterm = "EXW" if collected else "DAP"
    st.caption(
        "Customer collection: delivery is excluded from the quotation."
        if collected
        else "Delivery basis: DAP."
    )

    service = str(draft.get("transport_service", "Next Day"))
    booking = str(draft.get("transport_booking", "AM/PM"))
    vendor_preference = str(
        draft.get("transport_vendor_preference", "Highest available")
    )
    manual_override = bool(float(draft.get("transport_manual_override", 0) or 0))
    manual_transport_total = draft_number("transport_total")
    if delivery_method == "Haulier":
        delivery_columns = st.columns(2 if simple_mode else 3)
        col1, col2 = delivery_columns[:2]
        service = col1.selectbox(
            "Service",
            ["Economy", "Next Day"],
            index=["Economy", "Next Day"].index(service),
        )
        booking = col2.selectbox(
            "Booking",
            ["Standard", "AM/PM", "Timed"],
            index=["Standard", "AM/PM", "Timed"].index(booking),
        )
        if simple_mode:
            vendor_preference = "Highest available"
        else:
            preferences = [
                "Highest available",
                "Cheapest available",
                "Joda",
                "McDowells",
            ]
            vendor_preference = delivery_columns[2].selectbox(
                "Haulier",
                preferences,
                index=preferences.index(vendor_preference)
                if vendor_preference in preferences
                else 0,
            )
        if is_admin:
            manual_override = st.checkbox(
                "Use a manual transport total",
                value=manual_override,
            )
        else:
            manual_override = False
        if is_admin and manual_override:
            manual_transport_total = st.number_input(
                "Manual transport total (£)",
                min_value=0.0,
                value=manual_transport_total,
                step=1.0,
            )
        if not simple_mode:
            st.caption(
                "AM/PM, timed-booking and full-load charges are included automatically."
                if not is_admin
                else "AM/PM adds £7 per load; Timed adds £19 per load. McDowells adds £40 for each complete 26-pallet load."
            )

    calculate = st.button(
        "Continue to price" if simple_mode else "Calculate pricing base",
        type="primary",
        disabled=material_summary is None,
        width="stretch" if simple_mode else "content",
    )

    if calculate and material_summary is not None:
        updated = {
            **material_summary,
            "material": str(
                material_summary.get("board_material_spec", "")
                or draft.get("material", "")
                or ""
            ),
            "delivery_method": delivery_method,
            "incoterm": incoterm,
            "transport_service": service,
            "transport_booking": booking,
            "transport_vendor_preference": vendor_preference,
            "transport_manual_override": int(manual_override),
            "delivery_pallets_per_calloff": planned_pallets_per_delivery,
            "estimated_delivery_count": estimated_deliveries,
        }
        if selected_board is not None:
            updated.update(
                {
                    "board_gsm": float(selected_board["effective_gsm"]),
                    "board_width_mm": float(selected_board["effective_width_mm"]),
                    "board_length_mm": float(selected_board["effective_length_mm"]),
                    "board_code": str(selected_board.get("resolved_article_no", "")),
                }
            )
            if draft_number("net_mass_kg") <= 0:
                updated["net_mass_kg"] = float(
                    material_summary["board_tonnes_per_1000"]
                )
        try:
            if delivery_method == "Haulier" and not manual_override:
                quotes = rate_table.quote_schedule(
                    postcode=str(draft["delivery_postcode"]),
                    total_pallets=estimated_pallets,
                    pallets_per_delivery=planned_pallets_per_delivery,
                    service=service,
                    booking=booking,
                )
                if vendor_preference == "Highest available":
                    selected_quote = max(
                        quotes, key=lambda quote: float(quote.total_cost)
                    )
                elif vendor_preference == "Cheapest available":
                    selected_quote = min(
                        quotes, key=lambda quote: float(quote.total_cost)
                    )
                else:
                    selected_quote = next(
                        (quote for quote in quotes if quote.vendor == vendor_preference),
                        None,
                    )
                    if selected_quote is None:
                        raise TransportLookupError(
                            f"{vendor_preference} does not have a complete rate for this postcode and pallet count."
                        )
                updated.update(
                    {
                        "transport_vendor": selected_quote.vendor,
                        "transport_rate_zone": selected_quote.rate_zone,
                        "transport_total": selected_quote.total_cost,
                    }
                )
                st.session_state.transport_quotes = [
                    quote.to_dict() for quote in quotes
                ]
            elif delivery_method == "Haulier":
                updated.update(
                    {
                        "transport_vendor": "Manual override",
                        "transport_rate_zone": "Manual",
                        "transport_total": manual_transport_total,
                    }
                )
                st.session_state.transport_quotes = []
            else:
                updated.update(
                    {
                        "transport_vendor": "",
                        "transport_rate_zone": "",
                        "transport_total": 0.0,
                    }
                )
                st.session_state.transport_quotes = []

            st.session_state.draft.update(updated)
            st.session_state.material_lines = material_lines
            st.session_state.breakdown = calculate_cost(st.session_state.draft)
            st.session_state.pop("pricing", None)
            if simple_mode:
                navigate_to(3)
            else:
                st.rerun()
        except (TransportLookupError, ValueError) as exc:
            st.error(str(exc))

    if st.session_state.get("breakdown"):
        quotes = st.session_state.get("transport_quotes", [])
        if quotes and is_admin:
            quote_frame = pd.DataFrame(quotes).rename(
                columns={
                    "vendor": "Haulier",
                    "rate_zone": "Rate zone",
                    "delivery_count": "Deliveries",
                    "pallets_per_delivery": "Pallets / delivery",
                    "load_count": "Loads",
                    "base_cost": "Base cost",
                    "booking_surcharge": "Booking surcharge",
                    "full_load_surcharge": "Full-load surcharge",
                    "total_cost": "Total",
                }
            )
            st.dataframe(
                quote_frame[
                    [
                        "Haulier",
                        "Rate zone",
                        "Deliveries",
                        "Pallets / delivery",
                        "Loads",
                        "Base cost",
                        "Booking surcharge",
                        "Full-load surcharge",
                        "Total",
                    ]
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    column: st.column_config.NumberColumn(format="£%.2f")
                    for column in [
                        "Base cost",
                        "Booking surcharge",
                        "Full-load surcharge",
                        "Total",
                    ]
                },
            )
            st.success(
                f"Selected {draft.get('transport_vendor')} using rate zone {draft.get('transport_rate_zone')}: "
                f"£{float(draft.get('transport_total', 0)):,.2f} across "
                f"{int(draft.get('estimated_delivery_count', 1) or 1):,} planned delivery event(s)."
            )
        if is_admin:
            show_cost_breakdown(st.session_state.breakdown)
            show_admin_adjustment_detail(st.session_state.breakdown)
        elif not simple_mode:
            st.success("Material and delivery have been calculated.")
        if not simple_mode and st.button("Continue to pricing", type="primary"):
            navigate_to(3)


def sync_selling_from_spread() -> None:
    try:
        pricing = price_from_spread_percent(
            float(st.session_state.breakdown["pricing_base_per_1000"])
            * quote_exchange_factor(),
            float(st.session_state.spread_percent_input),
        )
        pricing = with_operational_spread(pricing)
        st.session_state.selling_price_input = pricing["selling_price_per_1000"]
        st.session_state.pricing = pricing
        st.session_state.draft["spread_percent"] = pricing["spread_percent"]
        st.session_state.pop("pricing_error", None)
    except ValueError as exc:
        st.session_state.pricing_error = str(exc)


def sync_spread_from_selling_price() -> None:
    try:
        pricing = spread_percent_from_price(
            float(st.session_state.breakdown["pricing_base_per_1000"])
            * quote_exchange_factor(),
            float(st.session_state.selling_price_input),
        )
        pricing = with_operational_spread(pricing)
        st.session_state.spread_percent_input = pricing["spread_percent"]
        st.session_state.pricing = pricing
        st.session_state.draft["spread_percent"] = pricing["spread_percent"]
        st.session_state.pop("pricing_error", None)
    except ValueError as exc:
        st.session_state.pricing_error = str(exc)


def show_machine_time_calculation(
    repository: CsvRepository,
    pricing: dict[str, float],
) -> None:
    draft = st.session_state.draft
    bom_code = (
        str(draft.get("source_item_code") or draft.get("item_code", ""))
        if float(draft.get("bom_available", 0) or 0)
        else str(draft.get("component_template_item_code", ""))
    )
    if not bom_code:
        return
    if not hasattr(repository, "machine_time_breakdown"):
        return
    details = repository.machine_time_breakdown(bom_code)
    lines = details["lines"]
    if lines.empty:
        return

    with st.expander("View machine hours calculation"):
        st.caption(
            f"Machine-time BOM: {bom_code}. For direct operations, hours per 1,000 "
            "equals run hours divided by effective quantity per run. Column Q is used "
            "when it is positive; system quantity is the fallback."
        )
        visible = lines.rename(
            columns={
                "line_type": "Line type",
                "operation": "Operation",
                "machine": "Machine / group",
                "run_hours": "Run hours",
                "system_quantity_per_run": "System quantity / run",
                "effective_quantity_per_run": "Effective quantity / run",
                "quantity_source": "Quantity used",
                "calculation": "Calculation",
                "hours_per_1000": "Hours / 1,000",
            }
        )
        st.dataframe(
            visible,
            hide_index=True,
            width="stretch",
            column_config={
                "Run hours": st.column_config.NumberColumn(format="%.6f"),
                "System quantity / run": st.column_config.NumberColumn(format="%.6f"),
                "Effective quantity / run": st.column_config.NumberColumn(format="%.6f"),
                "Hours / 1,000": st.column_config.NumberColumn(format="%.6f"),
            },
        )
        st.caption(
            f"Total: {float(details['summary']['machine_hours_per_1000']):,.6f} hours per 1,000 "
            f"({format_machine_duration(details['summary']['machine_hours_per_1000'], include_seconds=True)}) "
            f"× {draft_number('order_quantity') / 1_000:,.3f} thousand units "
            f"= {pricing['total_machine_hours']:,.4f} machine hours for this quote "
            f"({format_machine_duration(pricing['total_machine_hours'], include_seconds=True)})."
        )


def render_pricing(
    repository: CsvRepository,
    user_username: str,
    user_email: str,
    user_name: str,
    is_admin: bool,
    simple_mode: bool = False,
) -> None:
    st.subheader("Set spread or selling price")
    draft = st.session_state.draft
    st.session_state.setdefault(
        "additional_charge_description",
        str(draft.get("additional_charge_description", "Forme / Stereo")),
    )
    st.session_state.setdefault(
        "additional_charge_amount",
        float(draft.get("additional_charge_amount", DEFAULT_TOOLING_CHARGE) or 0),
    )
    st.session_state.setdefault(
        "additional_charge_foc",
        bool(draft.get("additional_charge_foc", False)),
    )
    draft["additional_charge_description"] = st.session_state.additional_charge_description
    draft["additional_charge_amount"] = st.session_state.additional_charge_amount
    draft["additional_charge_foc"] = st.session_state.additional_charge_foc
    breakdown = st.session_state.breakdown
    expected_tooling_amortisation = (
        FOC_TOOLING_AMORTISATION_PER_1000
        if st.session_state.additional_charge_foc
        else TOOLING_AMORTISATION_PER_1000
    )
    current_tooling_amortisation = float(
        breakdown.get("tooling_amortisation_per_1000", 0) or 0
    )
    if current_tooling_amortisation != float(expected_tooling_amortisation):
        pricing_base = float(breakdown.get("pricing_base_per_1000", 0) or 0)
        pricing_base += expected_tooling_amortisation - current_tooling_amortisation
        breakdown = {
            **breakdown,
            "tooling_amortisation_per_1000": expected_tooling_amortisation,
            "pricing_base_per_1000": round(pricing_base, 4),
            "pricing_base_per_item": round(pricing_base / 1_000, 5),
        }
        st.session_state.breakdown = breakdown
    if is_admin:
        show_cost_breakdown(breakdown)
        show_admin_adjustment_detail(breakdown)
        st.info(
            "Spread = (selling price − pricing base) ÷ selling price. "
            "Change either figure to recalculate the other."
        )
    else:
        st.caption("Enter either the spread or selling price. The other figure will update.")

    currency_options = ["GBP", "EUR"]
    current_currency = str(
        st.session_state.draft.get("quote_currency", "GBP") or "GBP"
    ).upper()
    if current_currency not in currency_options:
        current_currency = "GBP"
    currency_col, exchange_col = st.columns(2)
    quote_currency = currency_col.selectbox(
        "Quotation currency",
        currency_options,
        index=currency_options.index(current_currency),
        help="GBP is the normal currency. Choose EUR only when the quotation is to be raised in euros.",
    )
    eur_per_gbp = 1.0
    eur_rate_date = ""
    eur_rate_source = ""
    if quote_currency == "EUR":
        try:
            eur_per_gbp, eur_rate_date = live_eur_per_gbp()
        except RuntimeError as exc:
            exchange_col.error(str(exc))
            st.session_state.draft["eur_per_gbp"] = 0.0
            st.session_state.draft["eur_rate_date"] = ""
            st.session_state.draft["eur_rate_source"] = ""
            return
        eur_rate_source = "ECB via Frankfurter"
        exchange_col.metric("Live rate · EUR per GBP", f"{eur_per_gbp:.4f}")
        exchange_col.caption(
            "ECB reference rate. "
            "Internal costs and the £600/hour gate remain in GBP."
        )
    else:
        exchange_col.text_input("Conversion rate", value="Not required for GBP", disabled=True)
    st.session_state.draft["quote_currency"] = quote_currency
    st.session_state.draft["eur_per_gbp"] = eur_per_gbp
    st.session_state.draft["eur_rate_date"] = eur_rate_date
    st.session_state.draft["eur_rate_source"] = eur_rate_source
    symbol = currency_symbol(quote_currency)
    pricing_base_gbp = float(breakdown["pricing_base_per_1000"])
    pricing_base = pricing_base_gbp * quote_exchange_factor()
    stored_pricing = st.session_state.get("pricing") or {}
    try:
        inputs_match_pricing = (
            abs(
                float(st.session_state.get("spread_percent_input"))
                - float(stored_pricing.get("spread_percent"))
            )
            < 0.005
            and abs(
                float(st.session_state.get("selling_price_input"))
                - float(stored_pricing.get("selling_price_per_1000"))
            )
            < 0.005
        )
    except (TypeError, ValueError):
        inputs_match_pricing = False
    pricing_is_complete = {
        "spread_percent",
        "selling_price_per_1000",
        "material_spread_value_per_1000",
        "spread_per_machine_hour",
    }.issubset(stored_pricing)
    if (
        st.session_state.get("pricing_base_for_inputs")
        != (pricing_base, quote_currency, round(eur_per_gbp, 8))
        or not stored_pricing
        or not pricing_is_complete
        or not inputs_match_pricing
    ):
        starting_spread = float(
            stored_pricing.get(
                "spread_percent", draft_number("spread_percent", 30)
            )
        )
        try:
            pricing = price_from_spread_percent(pricing_base, starting_spread)
        except ValueError:
            pricing = price_from_spread_percent(pricing_base, 0.0)
        pricing = with_operational_spread(pricing)
        st.session_state.pricing_base_for_inputs = (
            pricing_base,
            quote_currency,
            round(eur_per_gbp, 8),
        )
        st.session_state.spread_percent_input = pricing["spread_percent"]
        st.session_state.selling_price_input = pricing[
            "selling_price_per_1000"
        ]
        st.session_state.pricing = pricing
        st.session_state.draft["spread_percent"] = pricing["spread_percent"]

    left, right = st.columns(2)
    left.number_input(
        "Spread (%)",
        min_value=-100_000.0,
        max_value=99.99,
        step=0.5,
        format="%.2f",
        key="spread_percent_input",
        on_change=sync_selling_from_spread,
    )
    right.number_input(
        f"Selling price per 1,000 ({symbol})",
        min_value=0.01,
        step=1.0,
        format="%.2f",
        key="selling_price_input",
        on_change=sync_spread_from_selling_price,
    )

    if st.session_state.get("pricing_error"):
        st.error(st.session_state.pricing_error)

    pricing = st.session_state.get("pricing")
    if pricing:
        pricing_cards = [
            ("Selling price / 1,000", f"{symbol}{pricing['selling_price_per_1000']:,.2f}"),
            (
                "Selling price / item",
                format_unit_price(pricing["selling_price_per_item"], quote_currency),
            ),
            ("Spread", f"{pricing['spread_percent']:,.2f}%"),
        ]
        if simple_mode:
            pricing_cards.append(
                (
                    "Spread / machine hour",
                    f"£{pricing['spread_per_machine_hour']:,.2f}"
                    if pricing.get("total_machine_hours", 0) > 0
                    else "Not available",
                )
            )
        if is_admin:
            pricing_cards.append(
                (
                    "Spread value / 1,000",
                    f"{symbol}{pricing['spread_value_per_1000']:,.2f}",
                )
            )
        show_detail_cards(pricing_cards)
        if not simple_mode:
            st.markdown("#### Material-only operational spread")
        if not simple_mode and pricing.get("total_machine_hours", 0) > 0:
            operational_cards = [
                ("Spread / machine hour", f"£{pricing['spread_per_machine_hour']:,.2f}"),
                (
                    "Machine time for quote",
                    f"{pricing['total_machine_hours']:,.2f} h · "
                    f"{format_machine_duration(pricing['total_machine_hours'])}",
                ),
            ]
            if is_admin:
                operational_cards.extend(
                    [
                        (
                            "Material spread / 1,000",
                            f"£{pricing['material_spread_value_per_1000']:,.2f}",
                        ),
                        (
                            "Material spread for quote",
                            f"£{pricing['total_spread_value']:,.2f}",
                        ),
                    ]
                )
            show_detail_cards(operational_cards)
            if is_admin:
                st.caption(
                    f"Spread/hour uses the adjusted material base only: "
                    f"£{float(breakdown.get('material_base_per_1000', breakdown.get('materials_cost_per_1000', 0))):,.2f} "
                    f"per 1,000 at {pricing['spread_percent']:,.2f}%. Delivery is left out. Machine time: "
                    f"{st.session_state.draft.get('machine_time_source', 'BOM operation speeds')}."
                )
            else:
                st.caption("Spread per machine hour excludes delivery.")
            show_machine_time_calculation(repository, pricing)
        elif not simple_mode:
            st.info(
                "No machine time is available for this BOM, so spread/hour cannot be calculated."
            )
        if pricing["spread_percent"] < 0:
            st.warning("The selected selling price produces a negative spread.")

        st.markdown("#### One-off tooling")
        st.caption(
            f"Tooling defaults to {currency_symbol(quote_currency)}"
            f"{DEFAULT_TOOLING_CHARGE:,.0f} per item. A £"
            f"{TOOLING_AMORTISATION_PER_1000:,.0f} per 1,000 tooling allowance is "
            f"included in the pricing base; selecting FOC doubles it to £"
            f"{FOC_TOOLING_AMORTISATION_PER_1000:,.0f} per 1,000. The separate "
            "one-off charge is not included in the material-only spread/hour test."
        )
        tooling_left, tooling_middle, tooling_right = st.columns([2, 1, 1])
        tooling_left.text_input(
            "Charge description", key="additional_charge_description"
        )
        tooling_middle.number_input(
            f"Charge ({symbol})",
            min_value=0.0,
            step=25.0,
            key="additional_charge_amount",
            disabled=bool(st.session_state.additional_charge_foc),
        )
        tooling_right.checkbox("FOC", key="additional_charge_foc")

        traffic = traffic_light_result(
            pricing.get("spread_per_machine_hour", 0),
            pricing.get("spread_percent", 0),
        )
        basis = traffic_override_basis(pricing)
        pricing["traffic_light_status"] = traffic["status"]
        pricing["traffic_light_reason"] = traffic["reason"]
        if pricing.get("traffic_override_basis") != basis:
            for key in [
                "traffic_override_approved",
                "traffic_override_reason",
                "traffic_override_by_username",
                "traffic_override_by_name",
                "traffic_override_by_email",
                "traffic_override_at_utc",
                "traffic_override_basis",
            ]:
                pricing.pop(key, None)
        if pricing.get("traffic_amber_acknowledgement_basis") != basis:
            for key in [
                "traffic_amber_acknowledged",
                "traffic_amber_acknowledged_by_username",
                "traffic_amber_acknowledged_at_utc",
                "traffic_amber_acknowledgement_basis",
            ]:
                pricing.pop(key, None)
        if traffic["status"] != "red":
            pricing["traffic_override_approved"] = False
        if traffic["status"] != "amber":
            pricing["traffic_amber_acknowledged"] = False
        st.session_state.pricing = pricing

        st.markdown("#### Commercial check")
        if traffic["status"] == "green":
            st.success(
                f"GREEN — both targets are met. {traffic['reason'].capitalize()}."
            )
        elif traffic["status"] == "amber":
            st.markdown(
                '<div class="amber-alert"><strong>⚠ AMBER COMMERCIAL WARNING</strong>'
                "The hourly target is met, but spread is below 30%. Review the "
                "selling price before continuing. The configured amber approver "
                "will sign before the Customer.</div>",
                unsafe_allow_html=True,
            )
        else:
            st.error(
                f"RED — Sales Director or delegated individual approval is required. "
                f"{traffic['reason'].capitalize()}. The Director will sign first in "
                "Dropbox Sign before the Customer."
            )

        amber_acknowledged = bool(pricing.get("traffic_amber_acknowledged"))
        if traffic["status"] == "amber":
            if amber_acknowledged:
                st.success(
                    "Amber warning acknowledged by "
                    f"@{pricing.get('traffic_amber_acknowledged_by_username', user_username)}."
                )
            elif st.button("Acknowledge amber warning", type="primary"):
                pricing.update(
                    {
                        "traffic_amber_acknowledged": True,
                        "traffic_amber_acknowledged_by_username": user_username,
                        "traffic_amber_acknowledged_at_utc": _utc_now().isoformat(),
                        "traffic_amber_acknowledgement_basis": basis,
                    }
                )
                st.session_state.pricing = pricing
                st.rerun()

        can_continue = (
            traffic["status"] == "green"
            or (traffic["status"] == "amber" and amber_acknowledged)
            or traffic["status"] == "red"
        )
        if st.button(
            "Continue to save and send",
            type="primary",
            disabled=not can_continue,
        ):
            navigate_to(4)


def current_record() -> dict[str, Any]:
    record = {
        **st.session_state.draft,
        **st.session_state.breakdown,
        **st.session_state.pricing,
        "quote_reference": st.session_state.get("quote_reference", ""),
        "quote_number": st.session_state.get("quote_number", ""),
        "quote_revision": st.session_state.get("quote_revision", ""),
        "customer_contact": st.session_state.get("customer_contact", ""),
        "customer_role": st.session_state.get("customer_role", ""),
        "customer_email": st.session_state.get("customer_email", ""),
        "director_name": st.session_state.get("director_name", ""),
        "director_email": st.session_state.get("director_email", ""),
        "approval_recipient_name": st.session_state.get(
            "approval_recipient_name", ""
        ),
        "approval_recipient_email": st.session_state.get(
            "approval_recipient_email", ""
        ),
        "approval_recipient_role": st.session_state.get(
            "approval_recipient_role", ""
        ),
        "approval_recipient_is_cover": st.session_state.get(
            "approval_recipient_is_cover", False
        ),
        "notes": st.session_state.get("quote_notes", ""),
        "additional_charge_description": st.session_state.get(
            "additional_charge_description", ""
        ),
        "additional_charge_amount": st.session_state.get(
            "additional_charge_amount", DEFAULT_TOOLING_CHARGE
        ),
        "additional_charge_foc": st.session_state.get(
            "additional_charge_foc", False
        ),
    }
    if record["additional_charge_foc"]:
        record["additional_charge_amount"] = 0.0
    if (
        record["additional_charge_foc"]
        or float(record["additional_charge_amount"] or 0) > 0
    ) and not str(record["additional_charge_description"] or "").strip():
        record["additional_charge_description"] = "Forme / Stereo"
    return record


SAVED_REVISION_FIELDS = [
    *SPECIFICATION_COLUMNS,
    *COST_INPUT_COLUMNS,
    *CALCULATION_COLUMNS,
    "source_item_code",
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
    "notes",
    "additional_charge_description",
    "additional_charge_amount",
    "additional_charge_foc",
    "is_multi_item_quote",
    "quote_items",
    "multi_delivery_mode",
    "quoted_value",
    "annual_revenue",
]


def valid_email(value: str) -> bool:
    value = str(value or "").strip()
    return "@" in value and "." in value.rsplit("@", 1)[-1]


def active_sales_rep_signature_metadata(
    repository: CsvRepository,
    *,
    username: str,
    name: str,
) -> dict[str, Any]:
    """Snapshot the user's current signature version into a saved revision."""
    signature = repository.get_active_user_signature(username)
    if not signature:
        return {}
    return {
        "sales_rep_signature_id": signature["signature_id"],
        "sales_rep_signature_name": str(name or username).strip(),
        "sales_rep_signature_applied_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "sales_rep_signature_sha256": signature["image_sha256"],
    }


def with_sales_rep_signature(
    repository: CsvRepository,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Hydrate a quotation only with the signature version owned by its creator."""
    prepared = dict(record)
    signature_id = str(prepared.get("sales_rep_signature_id", "") or "").strip()
    owner = str(prepared.get("created_by_username", "") or "").strip()
    if not signature_id or not owner:
        return prepared
    try:
        signature = repository.get_user_signature_version(
            signature_id,
            expected_username=owner,
        )
    except RepositoryBusyError:
        # A quotation must never fall back to another user's current signature.
        # Leave it unsigned if its exact recorded version cannot be loaded.
        return prepared
    if not signature:
        return prepared
    content = bytes(signature.get("image_png") or b"")
    expected_digest = str(prepared.get("sales_rep_signature_sha256", "") or "")
    if not content or (
        expected_digest and signature_sha256(content) != expected_digest
    ):
        return prepared
    prepared["_sales_rep_signature_png"] = content
    return prepared


def render_my_signature(repository: CsvRepository, user: Any) -> None:
    st.header("My signature")
    st.write(
        "Save your own signature here and it will be added to quotation revisions "
        "that you personally save. It cannot be selected or applied by another user."
    )
    if not repository.uses_database:
        st.warning("Neon storage is required for personal signatures.")
        return
    try:
        current = repository.get_active_user_signature(user.username)
    except RepositoryBusyError as exc:
        st.error(str(exc))
        return
    if current:
        st.success(
            "A signature is saved for your account. New quotation revisions will "
            "record this exact version."
        )
        st.image(
            bytes(current.get("image_png") or b""),
            caption=f"Signature for {user.name}",
            width=360,
        )
        st.caption(
            "Saved "
            + format_uk_datetime(
                current.get("created_at_utc"), include_time=False, default=""
            )
        )
    else:
        st.info("No signature is currently saved for your account.")

    uploaded = st.file_uploader(
        "Upload a signature image",
        type=["png", "jpg", "jpeg"],
        help="A clear signature on a plain white background works best.",
    )
    processed: bytes | None = None
    if uploaded is not None:
        try:
            processed = normalise_signature_image(uploaded.getvalue())
        except SignatureImageError as exc:
            st.error(str(exc))
        else:
            st.caption("Preview")
            st.image(processed, width=360)
    consent = st.checkbox(
        "I confirm this is my signature and authorise the costing tool to apply it "
        "to quotation revisions that I personally save or send."
    )
    if st.button(
        "Save my signature",
        type="primary",
        disabled=processed is None or not consent,
    ):
        try:
            repository.save_user_signature(
                user.username,
                processed or b"",
                actor_username=user.username,
            )
        except (RepositoryBusyError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.success("Your signature has been saved.")
            st.rerun()

    if current:
        st.divider()
        remove_confirmed = st.checkbox(
            "I understand that removing this stops it being used on new revisions.",
            key="remove_signature_confirmed",
        )
        if st.button(
            "Remove my signature",
            disabled=not remove_confirmed,
        ):
            try:
                repository.remove_user_signature(
                    user.username,
                    actor_username=user.username,
                )
            except (RepositoryBusyError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success("Your signature has been removed from future revisions.")
                st.rerun()


def render_esign_test(
    repository: CsvRepository,
    saved: dict[str, Any],
    *,
    user_username: str,
    user_email: str,
    user_name: str,
) -> None:
    settings = configured_esign()
    api_key = str(settings.get("api_key", "") or "").strip()
    if not api_key:
        return
    saved = with_sales_rep_signature(repository, saved)
    st.markdown("#### Test e-signature")
    st.caption(
        "This route is locked to Dropbox Sign test mode. It sends real test emails, "
        "but the watermarked document is not legally binding."
    )
    customer_name = str(saved.get("customer_contact") or saved.get("customer_name") or "").strip()
    customer_role = str(saved.get("customer_role", "") or "").strip()
    customer_email = str(saved.get("customer_email", "") or "").strip()
    traffic_status = str(saved.get("traffic_light_status", "") or "").lower()
    requires_internal_approval = traffic_status in {"amber", "red"}
    try:
        current_approval_recipient = commercial_approval_recipient(
            settings, traffic_status
        )
    except ESignError as exc:
        st.warning(str(exc))
        return
    approval_name = str(
        current_approval_recipient.name
        if current_approval_recipient
        else saved.get("approval_recipient_name") or saved.get("director_name") or ""
    ).strip()
    approval_email = str(
        current_approval_recipient.email
        if current_approval_recipient
        else saved.get("approval_recipient_email") or saved.get("director_email") or ""
    ).strip()
    approval_role = str(
        current_approval_recipient.role
        if current_approval_recipient
        else saved.get("approval_recipient_role", "") or ""
    ).strip()
    if not approval_role and traffic_status == "red":
        approval_role = "Sales Director or delegated individual"
    elif not approval_role and traffic_status == "amber":
        approval_role = "Amber commercial approver"
    has_sales_rep_signature = bool(saved.get("_sales_rep_signature_png"))
    record_owner = str(saved.get("created_by_username", "") or "").strip()
    if record_owner and record_owner.casefold() != user_username.casefold():
        st.warning("Only the salesperson who saved this revision can send it for signature.")
        return
    request_id = str(saved.get("esign_request_id", "") or "").strip()
    status = str(saved.get("esign_status", "") or "").strip()

    if request_id:
        st.info(f"Dropbox Sign test status: **{status or 'sent'}**")
        for signer in saved.get("esign_signers", []) or []:
            signed_at = format_esign_datetime(signer.get("signed_at"))
            signer_status = str(signer.get("status", "unknown")).replace("_", " ")
            st.write(
                f"{signer.get('name') or signer.get('email')}: "
                f"{signer_status}"
                + (f" · {signed_at}" if signed_at else "")
            )
        status_columns = st.columns(2)
        if status_columns[0].button("Refresh signing status", width="stretch"):
            try:
                latest = DropboxSignClient(api_key).get_request(request_id)
                updated = repository.update_costing_esign(
                    str(saved.get("costing_id", "")),
                    latest,
                    owner_email=user_email,
                )
            except (ESignError, RepositoryBusyError) as exc:
                st.error(str(exc))
            else:
                st.session_state.last_saved = updated
                st.rerun()
        if bool(saved.get("esign_is_complete")):
            try:
                signed_pdf = DropboxSignClient(api_key).download_pdf(request_id)
            except ESignError as exc:
                status_columns[1].warning(str(exc))
            else:
                status_columns[1].download_button(
                    "Download completed test PDF",
                    data=signed_pdf,
                    file_name=f"{saved.get('quote_reference') or 'quotation'}-signed-test.pdf",
                    mime="application/pdf",
                    width="stretch",
                )
        return

    problems = []
    if not customer_name:
        problems.append("customer contact name")
    if not customer_role:
        problems.append("customer contact role")
    if not valid_email(customer_email):
        problems.append("customer email")
    if not has_sales_rep_signature:
        problems.append(
            "your saved signature (open My signature, then save a new quotation revision)"
        )
    if requires_internal_approval and not approval_name:
        problems.append(f"{approval_role or 'commercial approver'} name")
    if requires_internal_approval and not valid_email(approval_email):
        problems.append(f"{approval_role or 'commercial approver'} email")
    if customer_email.casefold() == user_email.casefold() and customer_email:
        problems.append("different Sales Representative and Customer email addresses")
    if (
        requires_internal_approval
        and customer_email.casefold() == approval_email.casefold()
        and customer_email
    ):
        problems.append("different commercial approver and Customer email addresses")
    if problems:
        st.warning("Save this revision with " + ", ".join(problems) + " before sending it.")
        return

    if requires_internal_approval:
        st.caption(
            f"Your saved signature is already on the quotation. {approval_name} "
            f"({approval_email}), {approval_role}, will sign first, followed by the Customer. A completed "
            f"copy will be emailed to {user_email}."
        )
    else:
        st.caption(
            "Your saved signature is already on the quotation, so only the Customer "
            f"needs to sign. A completed copy will be emailed to {user_email}."
        )
    approved = st.checkbox(
        "I approve this exact saved quotation and want it sent for the remaining "
        "signature(s)."
    )
    if st.button(
        "Approve and send test quotation",
        type="primary",
        disabled=not approved,
        width="stretch",
    ):
        approved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        approval = {
            "esign_status": "sending",
            "esign_test_mode": True,
            "esign_approved_by_username": user_username,
            "esign_approved_by_name": user_name,
            "esign_approved_by_email": user_email,
            "esign_approved_at_utc": approved_at,
            "esign_internal_signer_role": (
                approval_role if requires_internal_approval else "sales_representative_presigned"
            ),
            "approval_recipient_name": approval_name,
            "approval_recipient_email": approval_email,
            "approval_recipient_role": approval_role,
            "approval_recipient_is_cover": bool(
                current_approval_recipient and current_approval_recipient.is_cover
            ),
            "director_name": approval_name if requires_internal_approval else "",
            "director_email": approval_email if requires_internal_approval else "",
        }
        try:
            approved_record = repository.update_costing_esign(
                str(saved.get("costing_id", "")), approval, owner_email=user_email
            )
        except RepositoryBusyError as exc:
            st.error(str(exc))
            return
        try:
            approved_record = with_sales_rep_signature(repository, approved_record)
            use_generic_approval_page = requires_internal_approval and (
                traffic_status == "amber"
                or bool(approved_record.get("approval_recipient_is_cover"))
            )
            signature_pdf = quote_pdf(
                {
                    **approved_record,
                    "traffic_light_status": "amber",
                }
                if use_generic_approval_page
                else approved_record,
                esign_tags=not use_generic_approval_page,
            )
            if use_generic_approval_page:
                signature_pdf = append_commercial_signature_page(
                    signature_pdf,
                    approval_role=approval_role,
                    customer_role=customer_role,
                )
            result = DropboxSignClient(api_key).send_test_request(
                signature_pdf,
                title=f"Solidus quotation {saved.get('quote_reference') or saved.get('costing_id')}",
                subject=f"Test signature request: Solidus quotation {saved.get('quote_reference') or ''}",
                message=(
                    "This is a non-binding test of the Solidus quotation signing process. "
                    + (
                        f"The {approval_role} is asked to sign first, "
                        "followed by the Customer."
                        if requires_internal_approval
                        else "The Sales Representative has approved the quotation in the "
                        "costing tool; the Customer is asked to sign."
                    )
                ),
                director=(
                    Signer(approval_name, approval_email, 0)
                    if requires_internal_approval
                    else None
                ),
                customer=Signer(
                    customer_name,
                    customer_email,
                    1 if requires_internal_approval else 0,
                ),
                cc_email=user_email,
                costing_id=str(saved.get("costing_id", "")),
                quote_reference=str(saved.get("quote_reference", "")),
            )
        except ESignError as exc:
            st.error(str(exc))
            return
        try:
            updated = repository.update_costing_esign(
                str(saved.get("costing_id", "")), result, owner_email=user_email
            )
        except RepositoryBusyError as exc:
            # The external request exists, so retain its ID in this browser and
            # prevent an accidental duplicate even if Neon had a brief outage.
            st.session_state.last_saved = {**approved_record, **result}
            st.error(
                f"The test emails were sent, but their status was not saved to Neon: {exc} "
                "Do not send this revision again."
            )
        else:
            st.session_state.last_saved = updated
            st.success(
                "Test request sent. "
                + (
                    f"{approval_name} should receive the first email."
                    if requires_internal_approval
                    else "The Customer should receive the signing email."
                )
            )
            st.rerun()


def saved_revision_fingerprint(record: dict[str, Any]) -> str:
    """Identify the exact quoteable content represented by a saved revision."""
    normalised: dict[str, Any] = {}
    for field in SAVED_REVISION_FIELDS:
        value = record.get(field, "")
        if not pd.api.types.is_scalar(value):
            value = CsvRepository._json_ready(value)
        elif pd.isna(value):
            value = ""
        elif hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float):
            value = round(value, 10)
        normalised[field] = value
    payload = json.dumps(normalised, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def render_save(
    repository: CsvRepository,
    user_username: str,
    user_email: str,
    user_name: str,
    can_create_new: bool,
    is_admin: bool,
) -> None:
    st.subheader("Save, quote and export")
    simple_mode = not can_create_new and not is_admin
    draft = st.session_state.draft
    st.session_state.setdefault("quote_reference", "")
    st.session_state.setdefault("quote_number", "")
    st.session_state.setdefault("quote_revision", "")
    st.session_state.setdefault("customer_contact", "")
    st.session_state.setdefault("customer_role", "")
    esign_settings = configured_esign()
    st.session_state.setdefault("customer_email", "")
    st.session_state.setdefault("quote_notes", "")
    traffic_status = str(
        (st.session_state.get("pricing") or {}).get("traffic_light_status", "")
    ).lower()
    approval_error = ""
    try:
        approval_recipient = commercial_approval_recipient(
            esign_settings, traffic_status
        )
    except ESignError as exc:
        approval_recipient = None
        approval_error = str(exc)
    st.session_state.approval_recipient_name = (
        approval_recipient.name if approval_recipient else ""
    )
    st.session_state.approval_recipient_email = (
        approval_recipient.email if approval_recipient else ""
    )
    st.session_state.approval_recipient_role = (
        approval_recipient.role if approval_recipient else ""
    )
    st.session_state.approval_recipient_is_cover = bool(
        approval_recipient and approval_recipient.is_cover
    )
    # Retain the legacy fields while saved revisions transition to generic names.
    st.session_state.director_name = st.session_state.approval_recipient_name
    st.session_state.director_email = st.session_state.approval_recipient_email

    form_context = (
        st.form("external_quote_save", border=False)
        if simple_mode
        else nullcontext()
    )
    with form_context:
        left, right = st.columns(2)
        left.text_input(
            "Quote reference",
            value=st.session_state.quote_reference or "Assigned when saved",
            disabled=True,
        )
        right.text_input("Customer contact", key="customer_contact")
        st.text_input("Customer role", key="customer_role")
        if str(esign_settings.get("api_key", "") or "").strip():
            st.caption("Test e-sign recipients")
            st.text_input("Customer email", key="customer_email")
            if traffic_status in {"amber", "red"}:
                if approval_error:
                    st.warning(approval_error)
                elif st.session_state.approval_recipient_name and valid_email(
                    st.session_state.approval_recipient_email
                ):
                    st.caption(
                        f"{traffic_status.upper()} route: {user_name}'s saved signature "
                        f"will be applied. {st.session_state.approval_recipient_role} "
                        f"{st.session_state.approval_recipient_name} "
                        f"({st.session_state.approval_recipient_email}) will sign first."
                    )
                else:
                    st.warning(
                        "An administrator must set this route's commercial approver "
                        "name and email in Streamlit Secrets before it can be sent."
                    )
            else:
                st.caption(
                    f"{str((st.session_state.get('pricing') or {}).get('traffic_light_status', '')).upper()} "
                    f"route: {user_name}'s saved signature will be applied and only the "
                    "Customer will be asked to sign."
                )
        quote_notes = st.text_area("Quote notes", key="quote_notes", height=100)

        record = current_record()
        record["notes"] = quote_notes
        summary_cards = [
            ("Item", record["item_code"]),
            ("Quantity", f"{float(record['order_quantity']):,.0f}"),
            ("Fulfilment", record.get("fulfilment_type", "MTO")),
            (
                "Sell / 1,000",
                f"{currency_symbol(record.get('quote_currency'))}"
                f"{record['selling_price_per_1000']:,.2f}",
            ),
            (
                "Quote value",
                f"{currency_symbol(record.get('quote_currency'))}"
                f"{(float(record.get('selling_price_per_item', 0) or 0) * float(record.get('order_quantity', 0) or 0) + (0 if record.get('additional_charge_foc') else float(record.get('additional_charge_amount', 0) or 0))):,.2f}",
            ),
            (
                "Annual revenue",
                f"{currency_symbol(record.get('quote_currency'))}"
                f"{(float(record.get('selling_price_per_item', 0) or 0) * float(record.get('annual_volume_units', 0) or 0)):,.2f}",
            ),
        ]
        if is_admin:
            summary_cards.insert(
                3,
                (
                    "Pricing base / 1,000",
                    f"{currency_symbol(record.get('quote_currency'))}"
                    f"{record['pricing_base_per_1000'] * quote_exchange_factor(record):,.2f}",
                ),
            )
        show_detail_cards(summary_cards)
        save_submitted = (
            st.form_submit_button(
                "Save revision", type="primary", width="stretch"
            )
            if simple_mode
            else st.button(
                "Save as a new revision", type="primary", width="stretch"
            )
        )

    if save_submitted:
        record["source_item_code"] = draft.get("source_item_code", "")
        record["catalogue_product"] = bool(
            draft.get("catalogue_product", False)
            or (
                can_create_new
                and (
                    draft.get("based_on_existing_new_product", False)
                    or not str(draft.get("source_item_code", "")).strip()
                )
            )
        )
        try:
            record.update(
                active_sales_rep_signature_metadata(
                    repository,
                    username=user_username,
                    name=user_name,
                )
            )
            saved = repository.save_costing(
                record,
                user_username=user_username,
                user_email=user_email,
                user_name=user_name,
            )
        except RepositoryBusyError as exc:
            st.warning(str(exc))
        else:
            st.session_state.last_saved = saved
            st.session_state.quote_reference = str(saved.get("quote_reference", ""))
            st.session_state.quote_number = saved.get("quote_number", "")
            st.session_state.quote_revision = saved.get("quote_revision", "")
            cached_product_catalog.clear()
            st.session_state.saved_revision_fingerprint = (
                saved_revision_fingerprint(saved)
            )
            st.success(
                f"Saved as {saved['costing_id']} — "
                f"{saved['item_code']} revision {saved['revision']}."
            )

    st.markdown("#### Downloads")
    saved = st.session_state.get("last_saved")
    # Saving assigns the Neon quote reference, so rebuild the current record
    # before comparing it with the immutable saved revision.
    record = current_record()
    current_fingerprint = saved_revision_fingerprint(record)
    if (
        not saved
        or st.session_state.get("saved_revision_fingerprint")
        != current_fingerprint
    ):
        st.warning(
            "Save this revision before downloading or printing anything. "
            "If you change the quotation afterwards, save it again so the "
            "downloaded version is recorded in history."
        )
        return

    export_record = with_sales_rep_signature(repository, saved)
    download_count = 1 + int(can_create_new) + int(is_admin)
    download_columns = st.columns(download_count)
    download_columns[0].download_button(
        "Customer quote PDF",
        data=quote_pdf(export_record),
        file_name=f"{export_record['quote_reference'] or 'draft-quote'}.pdf",
        mime="application/pdf",
        width="stretch",
    )
    next_column = 1
    if can_create_new:
        download_columns[next_column].download_button(
            "Sage stock import CSV",
            data=sage_stock_import_csv(export_record),
            file_name=f"{export_record['item_code']}-sage-import.csv",
            mime="text/csv",
            width="stretch",
        )
        st.info(
            "The Sage download uses the standard 72-column stock import layout. "
            "A new plain board is included as a second row when one was entered. "
            "Check the account and product fields before importing."
        )
        next_column += 1
    if is_admin:
        one_row = pd.DataFrame([export_record]).to_csv(index=False).encode("utf-8-sig")
        download_columns[next_column].download_button(
            "Costing CSV",
            data=one_row,
            file_name=f"{export_record['item_code']}-costing.csv",
            mime="text/csv",
            width="stretch",
        )
    render_esign_test(
        repository,
        export_record,
        user_username=user_username,
        user_email=user_email,
        user_name=user_name,
    )


def load_saved_costing(record: dict[str, Any]) -> None:
    draft = default_draft()
    draft.update(
        {
            key: record.get(key, draft.get(key))
            for key in [*SPECIFICATION_COLUMNS, *COST_INPUT_COLUMNS]
        }
    )
    if "spread_percent" in record:
        draft["spread_percent"] = record["spread_percent"]
    draft["source_item_code"] = (
        record.get("source_item_code") or record.get("item_code") or ""
    )
    draft["catalogue_product"] = bool(record.get("catalogue_product", False))

    st.session_state.draft = clean_record(draft)
    reset_downstream()
    st.session_state.customer_contact = str(record.get("customer_contact", "") or "")
    st.session_state.customer_role = str(record.get("customer_role", "") or "")
    st.session_state.customer_email = str(record.get("customer_email", "") or "")
    st.session_state.director_name = str(record.get("director_name", "") or "")
    st.session_state.director_email = str(record.get("director_email", "") or "")
    st.session_state.approval_recipient_name = str(
        record.get("approval_recipient_name", "") or ""
    )
    st.session_state.approval_recipient_email = str(
        record.get("approval_recipient_email", "") or ""
    )
    st.session_state.approval_recipient_role = str(
        record.get("approval_recipient_role", "") or ""
    )
    st.session_state.approval_recipient_is_cover = bool(
        record.get("approval_recipient_is_cover", False)
    )
    st.session_state.quote_notes = str(record.get("notes", "") or "")
    st.session_state.quote_reference = str(record.get("quote_reference", "") or "")
    st.session_state.quote_number = record.get("quote_number", "") or ""
    st.session_state.quote_revision = record.get("quote_revision", "") or ""
    st.session_state.additional_charge_description = str(
        record.get("additional_charge_description", "Forme / Stereo")
        or "Forme / Stereo"
    )
    st.session_state.additional_charge_amount = float(
        record.get("additional_charge_amount", DEFAULT_TOOLING_CHARGE) or 0
    )
    st.session_state.additional_charge_foc = bool(
        record.get("additional_charge_foc", False)
    )
    st.session_state.workflow_notice = (
        f"Loaded {record.get('costing_id', 'saved costing')} revision "
        f"{int(float(record.get('revision', 0) or 0))}. "
        "Check the details, make your changes and save when you are done."
    )
    st.session_state.main_navigation = "Costing workflow"
    st.session_state.step = 1


def render_history(
    repository: CsvRepository,
    current_user: str,
    is_admin: bool,
) -> None:
    st.header("My costings")
    st.caption("These are the costings you have saved. Open one if you need to change it.")
    history = repository.load_user_history(current_user)
    if history.empty:
        st.info("You have not saved any costings yet.")
        return

    item_options = ["All products", *sorted(history["item_code"].dropna().unique())]
    selected_item = st.selectbox("Filter by product", item_options)

    filtered = history.copy()
    if selected_item != "All products":
        filtered = filtered[filtered["item_code"] == selected_item]
    filtered = filtered.sort_values("created_at_utc", ascending=False)

    visible_columns = [
        "created_at_utc",
        "created_by_username",
        "item_code",
        "revision",
        "customer_name",
        "description",
        "fulfilment_type",
        "order_quantity",
        "quote_currency",
        "selling_price_per_1000",
        "spread_percent",
        "spread_per_machine_hour",
        "traffic_light_status",
        "traffic_override_by_username",
        "esign_status",
        "costing_id",
    ]
    if is_admin:
        visible_columns.insert(
            visible_columns.index("selling_price_per_1000"),
            "pricing_base_per_1000",
        )
    display_history = format_frame_dates(filtered[visible_columns], ["created_at_utc"])
    st.dataframe(
        display_history,
        hide_index=True,
        width="stretch",
        column_config={
            "created_at_utc": st.column_config.TextColumn("Saved"),
            "created_by_username": st.column_config.TextColumn("Username"),
            "pricing_base_per_1000": st.column_config.NumberColumn(format="£%.2f"),
            "quote_currency": st.column_config.TextColumn("Currency"),
            "selling_price_per_1000": st.column_config.NumberColumn(format="%.2f"),
            "spread_percent": st.column_config.NumberColumn(format="%.2f%%"),
            "order_quantity": st.column_config.NumberColumn(format="%.0f"),
            "traffic_light_status": st.column_config.TextColumn("Check"),
            "traffic_override_by_username": st.column_config.TextColumn(
                "Override by"
            ),
            "esign_status": st.column_config.TextColumn("E-sign"),
        },
    )

    labels = {
        str(row["costing_id"]): (
            f"{row['item_code']} · revision {int(float(row['revision']))} · "
            f"{row.get('customer_name', '')} · {format_uk_datetime(row['created_at_utc'])}"
        )
        for _, row in filtered.iterrows()
    }
    selected_id = st.selectbox(
        "Choose a costing to reopen",
        options=list(labels),
        format_func=labels.get,
    )
    selected_record = clean_record(
        filtered.loc[filtered["costing_id"].astype(str) == selected_id].iloc[0].to_dict()
    )
    with st.container(border=True):
        show_detail_cards(
            [
                ("Product", selected_record.get("item_code", "")),
                ("Revision", f"{float(selected_record.get('revision', 0)):,.0f}"),
                ("Quantity", f"{float(selected_record.get('order_quantity', 0)):,.0f}"),
                (
                    "Selling / item",
                    format_unit_price(
                        selected_record.get("selling_price_per_item"),
                        selected_record.get("quote_currency", "GBP"),
                    ),
                ),
            ]
        )
        st.write(str(selected_record.get("description", "")))
        st.button(
            "Load and amend this costing",
            type="primary",
            width="stretch",
            on_click=load_saved_costing,
            args=(selected_record,),
        )

    with st.expander("Download this list"):
        export_history = filtered if is_admin else filtered[visible_columns]
        csv_history = format_frame_dates(export_history, ["created_at_utc"])
        columns = st.columns(2)
        columns[0].download_button(
            "Download CSV",
            data=csv_history.to_csv(index=False).encode("utf-8-sig"),
            file_name="my-costing-history.csv",
            mime="text/csv",
            width="stretch",
        )
        columns[1].download_button(
            "Print-friendly PDF",
            data=history_pdf(export_history),
            file_name="my-costing-history.pdf",
            mime="application/pdf",
            width="stretch",
        )


def render_team_history(repository: CsvRepository, is_admin: bool) -> None:
    st.header("Team history")
    st.caption("This shows every saved costing and the username that saved it.")
    history = repository.load_history()
    if history.empty:
        st.info("No costings have been saved yet.")
        return

    history["created_by_username"] = (
        history["created_by_username"].fillna("").astype(str)
    )
    usernames = sorted(
        value for value in history["created_by_username"].unique() if value
    )
    left, right = st.columns(2)
    selected_user = left.selectbox("Filter by username", ["All users", *usernames])
    products = sorted(value for value in history["item_code"].dropna().unique() if value)
    selected_item = right.selectbox("Filter by product", ["All products", *products])

    filtered = history.copy()
    if selected_user != "All users":
        filtered = filtered[filtered["created_by_username"] == selected_user]
    if selected_item != "All products":
        filtered = filtered[filtered["item_code"] == selected_item]
    filtered = filtered.sort_values("created_at_utc", ascending=False)

    visible_columns = [
        "created_at_utc",
        "created_by_username",
        "created_by_name",
        "item_code",
        "revision",
        "customer_name",
        "fulfilment_type",
        "order_quantity",
        "quote_currency",
        "selling_price_per_1000",
        "spread_percent",
        "spread_per_machine_hour",
        "traffic_light_status",
        "traffic_override_by_username",
        "traffic_override_reason",
        "esign_status",
        "costing_id",
    ]
    display_history = format_frame_dates(filtered[visible_columns], ["created_at_utc"])
    st.dataframe(
        display_history,
        hide_index=True,
        width="stretch",
        column_config={
            "created_at_utc": st.column_config.TextColumn("Saved"),
            "created_by_username": st.column_config.TextColumn("Username"),
            "created_by_name": st.column_config.TextColumn("Name"),
            "quote_currency": st.column_config.TextColumn("Currency"),
            "selling_price_per_1000": st.column_config.NumberColumn(format="%.2f"),
            "spread_percent": st.column_config.NumberColumn(format="%.2f%%"),
            "spread_per_machine_hour": st.column_config.NumberColumn(format="£%.2f"),
            "traffic_light_status": st.column_config.TextColumn("Check"),
            "traffic_override_by_username": st.column_config.TextColumn(
                "Override by"
            ),
            "traffic_override_reason": st.column_config.TextColumn(
                "Override reason"
            ),
            "esign_status": st.column_config.TextColumn("E-sign"),
            "order_quantity": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.caption("Team history is view-only. Each user can reopen their own work from My costings.")
    with st.expander("Download this view"):
        export_history = filtered if is_admin else filtered[visible_columns]
        csv_history = format_frame_dates(export_history, ["created_at_utc"])
        columns = st.columns(2)
        columns[0].download_button(
            "Download CSV",
            data=csv_history.to_csv(index=False).encode("utf-8-sig"),
            file_name="team-costing-history.csv",
            mime="text/csv",
            width="stretch",
        )
        columns[1].download_button(
            "Print-friendly PDF",
            data=history_pdf(export_history),
            file_name="team-costing-history.pdf",
            mime="application/pdf",
            width="stretch",
        )


def render_user_management(
    repository: CsvRepository,
    current_username: str,
) -> None:
    st.subheader("Users and access")
    st.caption(
        "Users are held in Neon once they have been imported. Passwords are stored "
        "as one-way hashes."
    )
    if not repository.uses_database:
        st.warning("Connect Neon before managing users here.")
        return

    secret_users = configured_users_for_import()
    if secret_users:
        if st.button("Import missing users from Streamlit Secrets"):
            try:
                imported, available = repository.import_app_users(
                    secret_users,
                    actor_username=current_username,
                )
            except (RepositoryBusyError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"Imported {imported} of {available} configured user(s). "
                    "Existing Neon users were not changed."
                )
                st.rerun()

    users = repository.list_app_users()
    if users.empty:
        st.info(
            "There are no Neon users yet. Import the existing Secrets users first "
            "so the current administrator account is retained."
        )
        return

    role_names = {
        "external": "External — existing products and own history",
        "creator": "Creator — existing and new products",
        "admin": "Administrator — full access",
    }
    display = users.copy()
    display["role"] = display["role"].map(role_names).fillna(display["role"])
    display = format_frame_dates(display, ["last_login_at_utc"])
    st.dataframe(
        display[
            [
                "username", "name", "email", "role", "can_view_history",
                "is_active", "must_change_password", "last_login_at_utc",
            ]
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "username": st.column_config.TextColumn("Username"),
            "name": st.column_config.TextColumn("Name"),
            "email": st.column_config.TextColumn("Email"),
            "role": st.column_config.TextColumn("Access"),
            "can_view_history": st.column_config.CheckboxColumn("Team history"),
            "is_active": st.column_config.CheckboxColumn("Active"),
            "must_change_password": st.column_config.CheckboxColumn(
                "Password change due"
            ),
            "last_login_at_utc": st.column_config.TextColumn("Last login"),
        },
    )

    new_user_keys = (
        "new_user_username",
        "new_user_name",
        "new_user_email",
        "new_user_role",
        "new_user_can_view_history",
        "new_user_password",
    )
    if st.session_state.pop("clear_new_database_user_form", False):
        for key in new_user_keys:
            st.session_state.pop(key, None)

    with st.expander("Add a user"):
        with st.form("new_database_user"):
            username = st.text_input("Username *", key="new_user_username")
            name = st.text_input("Name *", key="new_user_name")
            email = st.text_input("Email *", key="new_user_email")
            role = st.selectbox(
                "Access level",
                list(role_names),
                format_func=role_names.get,
                key="new_user_role",
            )
            can_view_history = st.checkbox(
                "Can view team history", key="new_user_can_view_history"
            )
            password = st.text_input(
                "Temporary password *", type="password", key="new_user_password"
            )
            submitted = st.form_submit_button("Create user", type="primary")
        if submitted:
            if len(password) < 10:
                st.error("Use a temporary password of at least 10 characters.")
            else:
                try:
                    repository.save_app_user(
                        username=username,
                        email=email,
                        name=name,
                        password_hash=make_password_hash(password),
                        role=role,
                        can_view_history=can_view_history,
                        is_active=True,
                        must_change_password=True,
                        actor_username=current_username,
                    )
                except (RepositoryBusyError, ValueError) as exc:
                    st.error(str(exc))
                else:
                    st.success(f"Created @{username}. They must change the password at login.")
                    st.session_state.clear_new_database_user_form = True
                    st.rerun()

    st.markdown("#### Change a user")
    usernames = users["username"].astype(str).tolist()
    selected = st.selectbox("User", usernames, key="managed_username")
    selected_row = users.loc[users["username"].astype(str).eq(selected)].iloc[0]
    selected_role = str(selected_row.get("role", "external"))
    role_index = list(role_names).index(selected_role) if selected_role in role_names else 0
    try:
        login_security = repository.app_user_login_security(selected)
    except RepositoryBusyError as exc:
        st.warning(str(exc))
        login_security = {}
    if login_security.get("is_locked"):
        locked_until = format_uk_datetime(login_security.get("locked_until_utc"))
        st.warning(
            f"This account is temporarily locked after repeated failed sign-ins. "
            f"It will unlock at {locked_until}."
        )
        if st.button("Unlock account", key=f"unlock_{selected}"):
            try:
                repository.unlock_app_user(
                    selected,
                    actor_username=current_username,
                )
            except RepositoryBusyError as exc:
                st.error(str(exc))
            else:
                st.success(f"Unlocked @{selected}.")
                st.rerun()
    with st.form("edit_database_user"):
        edited_name = str(selected_row.get("name", ""))
        edited_email = str(selected_row.get("email", ""))
        st.text_input("Name", value=edited_name, disabled=True)
        st.text_input("Email", value=edited_email, disabled=True)
        edited_role = st.selectbox(
            "Access level",
            list(role_names),
            index=role_index,
            format_func=role_names.get,
            key="edited_access_level",
        )
        edited_history = st.checkbox(
            "Can view team history",
            value=bool(selected_row.get("can_view_history", False)),
            key="edited_team_history",
        )
        edited_active = st.checkbox(
            "Account active",
            value=bool(selected_row.get("is_active", True)),
        )
        replacement_password = st.text_input(
            "New temporary password (leave blank to keep the current one)",
            type="password",
        )
        update_submitted = st.form_submit_button("Save user changes")
    if update_submitted:
        if selected.casefold() == current_username.casefold() and not edited_active:
            st.error("You cannot disable the account you are currently using.")
        elif replacement_password and len(replacement_password) < 10:
            st.error("Use a temporary password of at least 10 characters.")
        else:
            try:
                repository.save_app_user(
                    username=selected,
                    email=edited_email,
                    name=edited_name,
                    password_hash=(
                        make_password_hash(replacement_password)
                        if replacement_password
                        else None
                    ),
                    role=edited_role,
                    can_view_history=edited_history,
                    is_active=edited_active,
                    must_change_password=bool(replacement_password),
                    actor_username=current_username,
                )
            except (RepositoryBusyError, ValueError) as exc:
                st.error(str(exc))
            else:
                if replacement_password and selected.casefold() == current_username.casefold():
                    _sign_out_local_session(
                        repository,
                        "Password reset. Sign in with the new password.",
                    )
                else:
                    st.success(f"Updated @{selected}.")
                    st.rerun()

    audit = repository.load_app_audit_log()
    if not audit.empty:
        with st.expander("Recent user changes"):
            display_audit = format_frame_dates(audit, ["occurred_at_utc"])
            st.dataframe(display_audit, hide_index=True, width="stretch")


def render_remote_approval_queue(repository: CsvRepository, user: Any) -> None:
    st.subheader("Commercial approvals")
    try:
        pending = repository.load_commercial_approval_requests("pending")
    except RuntimeError as exc:
        st.warning(str(exc))
        return
    if pending.empty:
        st.caption("There are no red costings waiting for approval.")
        return

    display = pending.copy()
    display["requested_at"] = display["requested_at_utc"].map(format_uk_datetime)
    display["spread"] = display["snapshot"].map(
        lambda value: float((value or {}).get("spread_percent", 0) or 0)
    )
    display["spread_per_hour"] = display["snapshot"].map(
        lambda value: float((value or {}).get("spread_per_machine_hour", 0) or 0)
    )
    st.dataframe(
        display[
            [
                "requested_at",
                "requester_name",
                "customer_name",
                "item_code",
                "spread",
                "spread_per_hour",
                "request_reason",
            ]
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "requested_at": st.column_config.TextColumn("Requested"),
            "requester_name": st.column_config.TextColumn("Requested by"),
            "customer_name": st.column_config.TextColumn("Customer"),
            "item_code": st.column_config.TextColumn("Item"),
            "spread": st.column_config.NumberColumn("Spread", format="%.2f%%"),
            "spread_per_hour": st.column_config.NumberColumn(
                "Spread / hour", format="£%.2f"
            ),
            "request_reason": st.column_config.TextColumn("Reason"),
        },
    )
    request_ids = pending["request_id"].astype(str).tolist()
    labels = {
        str(row["request_id"]): (
            f"{row['customer_name']} · {row['item_code']} · {row['requester_name']}"
        )
        for _, row in pending.iterrows()
    }
    selected_id = st.selectbox(
        "Request to review",
        request_ids,
        format_func=lambda value: labels.get(value, value),
    )
    selected = pending.loc[
        pending["request_id"].astype(str).eq(selected_id)
    ].iloc[0]
    snapshot = selected.get("snapshot") or {}
    quote_symbol = currency_symbol(snapshot.get("quote_currency", "GBP"))
    show_detail_cards(
        [
            ("Order quantity", f"{float(snapshot.get('order_quantity', 0) or 0):,.0f}"),
            ("Spread", f"{float(snapshot.get('spread_percent', 0) or 0):,.2f}%"),
            (
                "Selling price / 1,000",
                f"{quote_symbol}{float(snapshot.get('selling_price_per_1000', 0) or 0):,.2f}",
            ),
            (
                "Spread / machine hour",
                f"£{float(snapshot.get('spread_per_machine_hour', 0) or 0):,.2f}",
            ),
        ]
    )
    st.write(f"**User's reason:** {selected.get('request_reason', '')}")
    with st.form("commercial_approval_decision", clear_on_submit=True):
        admin_note = st.text_area("Admin note (optional)", height=80)
        approve_col, reject_col = st.columns(2)
        approve = approve_col.form_submit_button(
            "Approve costing", type="primary", width="stretch"
        )
        reject = reject_col.form_submit_button("Decline", width="stretch")
    if approve or reject:
        try:
            repository.decide_commercial_approval_request(
                selected_id,
                approved=approve,
                admin_username=user.username,
                admin_name=user.name,
                admin_email=user.email,
                decision_reason=admin_note.strip(),
            )
        except RuntimeError as exc:
            st.error(str(exc))
        else:
            st.success("Costing approved." if approve else "Approval declined.")
            st.rerun()


def render_admin_tools(
    repository: CsvRepository,
    user: Any,
) -> None:
    st.header("Admin tools")
    st.caption(
        "Manage user accounts, access and earlier costing-history data."
    )
    if repository.uses_database:
        render_user_management(repository, user.username)
        with st.expander("Import earlier CSV costing history"):
            st.caption(
                "This safely copies any revisions still present in saved_costings.csv. "
                "Costings already in Neon are skipped."
            )
            if st.button("Import CSV history into Neon"):
                try:
                    imported, available = repository.import_csv_history_to_database()
                except RepositoryBusyError as exc:
                    st.error(str(exc))
                else:
                    st.success(
                        f"Imported {imported:,} of {available:,} CSV revision(s). "
                        "Existing database records were left unchanged."
                    )
    else:
        st.warning(
            "Neon is not configured. Add the database URL in Streamlit Secrets "
            "before relying on saved history."
        )


def render_admin_activity(
    repository: CsvRepository,
    current_session_id: str,
    *,
    embedded: bool = False,
) -> None:
    if embedded:
        st.divider()
        st.subheader("Sessions")
    else:
        st.header("User activity")
    st.caption(
        "Live sessions and active time. Times shown are UK local time."
    )
    try:
        repository.expire_inactive_sessions(session_timeout_minutes())
    except RepositoryBusyError as exc:
        st.warning(str(exc))
    sessions = repository.load_sessions().copy()
    history = repository.load_history().copy()
    now = _utc_now()

    if sessions.empty:
        st.info("No sessions have been recorded since the app last started.")
        return

    for column in [
        "signed_in_at_utc",
        "last_activity_utc",
        "last_heartbeat_utc",
        "ended_at_utc",
    ]:
        sessions[f"_{column}"] = pd.to_datetime(
            sessions[column], utc=True, errors="coerce"
        )
    heartbeat_age = (now - sessions["_last_heartbeat_utc"]).dt.total_seconds()
    ended = sessions["ended_at_utc"].fillna("").astype(str).str.strip().ne("")
    forced = pd.to_numeric(sessions["force_logout"], errors="coerce").fillna(0).gt(0)
    sessions["status"] = "Inactive"
    sessions.loc[~ended & ~forced & heartbeat_age.le(90), "status"] = "Online"
    sessions.loc[forced & ~ended, "status"] = "Sign-out requested"
    sessions.loc[ended, "status"] = "Signed out"
    sessions["active_minutes"] = (
        pd.to_numeric(sessions["active_seconds"], errors="coerce").fillna(0) / 60
    ).round(1)
    sessions["signed_in"] = sessions["_signed_in_at_utc"].dt.tz_convert(
        "Europe/London"
    ).dt.strftime(
        "%d/%m/%Y %H:%M"
    )
    sessions["last_activity"] = sessions["_last_activity_utc"].dt.tz_convert(
        "Europe/London"
    ).dt.strftime(
        "%d/%m/%Y %H:%M"
    )

    today = now.tz_convert("Europe/London").date()
    signed_today = sessions["_signed_in_at_utc"].dt.tz_convert(
        "Europe/London"
    ).dt.date.eq(today)
    if history.empty:
        saved_today = 0
    else:
        saved_times = pd.to_datetime(history["created_at_utc"], utc=True, errors="coerce")
        saved_today = int(
            saved_times.dt.tz_convert("Europe/London").dt.date.eq(today).sum()
        )
    show_detail_cards(
        [
            ("Online now", str(int(sessions["status"].eq("Online").sum()))),
            ("Sessions today", str(int(signed_today.sum()))),
            ("Costings saved today", str(saved_today)),
            ("Saved costings", str(len(history))),
        ]
    )

    if not embedded:
        st.subheader("Sessions")
    current_sessions = sessions.loc[~ended].copy()
    visible = current_sessions.sort_values("_last_heartbeat_utc", ascending=False)[
        [
            "username",
            "name",
            "status",
            "current_page",
            "signed_in",
            "last_activity",
            "active_minutes",
        ]
    ]
    st.dataframe(
        visible,
        hide_index=True,
        width="stretch",
        column_config={
            "username": st.column_config.TextColumn("Username"),
            "name": st.column_config.TextColumn("Name"),
            "status": st.column_config.TextColumn("Status"),
            "current_page": st.column_config.TextColumn("Page"),
            "signed_in": st.column_config.TextColumn("Signed in"),
            "last_activity": st.column_config.TextColumn("Last activity"),
            "active_minutes": st.column_config.NumberColumn(
                "Active minutes", format="%.1f"
            ),
        },
    )

    signed_out_sessions = sessions.loc[ended].sort_values(
        "_ended_at_utc", ascending=False
    )
    if not signed_out_sessions.empty:
        with st.expander("Recent signed-out sessions"):
            st.dataframe(
                signed_out_sessions.head(25)[
                    [
                        "username",
                        "name",
                        "current_page",
                        "signed_in",
                        "last_activity",
                        "active_minutes",
                    ]
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    "username": st.column_config.TextColumn("Username"),
                    "name": st.column_config.TextColumn("Name"),
                    "current_page": st.column_config.TextColumn("Last page"),
                    "signed_in": st.column_config.TextColumn("Signed in"),
                    "last_activity": st.column_config.TextColumn("Last activity"),
                    "active_minutes": st.column_config.NumberColumn(
                        "Active minutes", format="%.1f"
                    ),
                },
            )

    open_sessions = sessions[
        ~ended
        & ~forced
        & sessions["session_id"].astype(str).ne(str(current_session_id))
    ].copy()
    if not open_sessions.empty:
        labels = {
            str(row["session_id"]): (
                f"@{row['username']} · {row['name']} · {row['status']} · "
                f"{row['last_activity']}"
            )
            for _, row in open_sessions.iterrows()
        }
        selected_session = st.selectbox(
            "Session to sign out",
            list(labels),
            format_func=labels.get,
        )
        if st.button("Force sign out", type="primary"):
            repository.force_logout_session(selected_session)
            st.success("Sign-out requested. It should take effect within 30 seconds.")
            st.rerun()
    else:
        st.caption("There are no other open sessions to sign out.")

    if not embedded:
        st.subheader("Saved work by user")
        if history.empty:
            st.info("No costings have been saved yet.")
        else:
            activity_work = history.copy()
            activity_work["created_by_username"] = (
                activity_work["created_by_username"].fillna("").astype(str)
            )
            activity_work["order_quantity"] = pd.to_numeric(
                activity_work["order_quantity"], errors="coerce"
            ).fillna(0)
            summary = (
                activity_work.groupby("created_by_username", as_index=False)
                .agg(
                    saved_costings=("costing_id", "count"),
                    products=("item_code", "nunique"),
                    customers=("customer_name", "nunique"),
                    quoted_units=("order_quantity", "sum"),
                    last_saved=("created_at_utc", "max"),
                )
                .sort_values("last_saved", ascending=False)
            )
            summary = format_frame_dates(summary, ["last_saved"])
            st.dataframe(summary, hide_index=True, width="stretch")

    st.caption(
        "Active time counts short gaps between actions, not simply an open browser tab. "
        "With Neon connected, this activity is retained when Streamlit restarts."
    )


def render_admin_dashboard(
    repository: CsvRepository,
    current_session_id: str,
) -> None:
    st.header("Dashboard")
    st.caption("Latest saved revision of each quotation.")
    history = repository.load_history()
    if history.empty:
        st.info("No quotations have been saved yet.")
        render_admin_activity(
            repository,
            current_session_id,
            embedded=True,
        )
        return

    work = history.copy()
    work["created_at_utc"] = pd.to_datetime(
        work["created_at_utc"], utc=True, errors="coerce"
    )
    quote_number = pd.to_numeric(work.get("quote_number"), errors="coerce")
    work["quotation_key"] = quote_number.map(
        lambda value: f"number:{int(value)}" if pd.notna(value) else ""
    )
    missing_key = work["quotation_key"].eq("")
    work.loc[missing_key, "quotation_key"] = (
        "legacy:" + work.loc[missing_key, "costing_id"].fillna("").astype(str)
    )
    work = (
        work.sort_values(["created_at_utc", "revision"])
        .drop_duplicates("quotation_key", keep="last")
        .copy()
    )

    period = st.selectbox(
        "Period",
        [
            "Today",
            "This week",
            "Last 30 days",
            "Last 90 days",
            "Last 12 months",
            "All time",
        ],
    )
    period_days = {
        "Last 30 days": 30,
        "Last 90 days": 90,
        "Last 12 months": 365,
    }.get(period)
    now_uk = pd.Timestamp.now(tz="Europe/London")
    if period == "Today":
        cutoff = now_uk.normalize().tz_convert("UTC")
        work = work[work["created_at_utc"].ge(cutoff)]
    elif period == "This week":
        cutoff = (
            now_uk.normalize() - pd.Timedelta(days=now_uk.weekday())
        ).tz_convert("UTC")
        work = work[work["created_at_utc"].ge(cutoff)]
    elif period_days:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=period_days)
        work = work[work["created_at_utc"].ge(cutoff)]

    user_options = sorted(
        work["created_by_username"].fillna("").astype(str).loc[lambda s: s.ne("")].unique()
    )
    selected_users = st.multiselect("Users", user_options)
    if selected_users:
        work = work[work["created_by_username"].isin(selected_users)]
    if work.empty:
        st.info("No quotations match those filters.")
        render_admin_activity(
            repository,
            current_session_id,
            embedded=True,
        )
        return

    for column in [
        "order_quantity",
        "annual_volume_units",
        "selling_price_per_1000",
        "selling_price_per_item",
        "spread_percent",
        "spread_per_machine_hour",
        "additional_charge_amount",
    ]:
        work[column] = pd.to_numeric(work.get(column), errors="coerce").fillna(0)
    foc = work.get("additional_charge_foc", False)
    if not isinstance(foc, pd.Series):
        foc = pd.Series(bool(foc), index=work.index)
    foc = foc.fillna(False).astype(bool)
    quote_currency = work.get("quote_currency", "GBP")
    if not isinstance(quote_currency, pd.Series):
        quote_currency = pd.Series(str(quote_currency or "GBP"), index=work.index)
    quote_currency = quote_currency.fillna("GBP").astype(str).str.upper()
    eur_per_gbp = pd.to_numeric(work.get("eur_per_gbp", 1.0), errors="coerce")
    if not isinstance(eur_per_gbp, pd.Series):
        eur_per_gbp = pd.Series(float(eur_per_gbp or 1.0), index=work.index)
    eur_per_gbp = eur_per_gbp.fillna(1.0).where(lambda values: values.gt(0), 1.0)
    calculated_quoted_value = (
        work["selling_price_per_1000"] * work["order_quantity"] / 1_000
        + work["additional_charge_amount"].where(~foc, 0)
    )
    stored_quoted_value = pd.to_numeric(
        work.get("quoted_value"), errors="coerce"
    )
    if not isinstance(stored_quoted_value, pd.Series):
        stored_quoted_value = pd.Series(float("nan"), index=work.index)
    work["quoted_value_in_quote_currency"] = stored_quoted_value.where(
        stored_quoted_value.gt(0), calculated_quoted_value
    )
    work["quoted_value"] = work["quoted_value_in_quote_currency"].where(
        quote_currency.ne("EUR"),
        work["quoted_value_in_quote_currency"] / eur_per_gbp,
    )
    calculated_annual_revenue = (
        work["selling_price_per_item"] * work["annual_volume_units"]
    )
    stored_annual_revenue = pd.to_numeric(
        work.get("annual_revenue"), errors="coerce"
    )
    if not isinstance(stored_annual_revenue, pd.Series):
        stored_annual_revenue = pd.Series(float("nan"), index=work.index)
    work["annual_revenue_in_quote_currency"] = stored_annual_revenue.where(
        stored_annual_revenue.gt(0), calculated_annual_revenue
    )
    work["annual_revenue"] = work["annual_revenue_in_quote_currency"].where(
        quote_currency.ne("EUR"),
        work["annual_revenue_in_quote_currency"] / eur_per_gbp,
    )
    traffic = work["traffic_light_status"].fillna("").astype(str).str.lower()
    complete = work["esign_is_complete"].fillna(False).astype(bool)
    esign_request_id = work["esign_request_id"].fillna("").astype(str).str.strip()
    esign_status = work["esign_status"].fillna("").astype(str).str.strip()
    esign_requested = esign_request_id.ne("") | esign_status.ne("")
    signed_count = int(complete.sum())
    signed_value = float(work.loc[complete, "quoted_value"].sum())
    quote_conversion = signed_count / len(work) * 100 if len(work) else 0.0
    total_quoted_value = float(work["quoted_value"].sum())
    total_annual_revenue = float(work["annual_revenue"].sum())
    value_conversion = (
        signed_value / total_quoted_value * 100 if total_quoted_value > 0 else 0.0
    )

    metrics = st.columns(6)
    metrics[0].metric("Quotations", f"{len(work):,}")
    metrics[1].metric("Quoted value", f"£{total_quoted_value:,.0f}")
    metrics[2].metric("Annual revenue", f"£{total_annual_revenue:,.0f}")
    metrics[3].metric("Average spread", f"{work['spread_percent'].mean():,.1f}%")
    metrics[4].metric(
        "Average spread / hour",
        f"£{work['spread_per_machine_hour'].mean():,.0f}",
    )
    metrics[5].metric("E-sign requests", f"{int(esign_requested.sum()):,}")

    st.subheader("Conversion")
    conversion_columns = st.columns(4)
    conversion_columns[0].metric("Signed quotations", f"{signed_count:,}")
    conversion_columns[1].metric("Signed value", f"£{signed_value:,.0f}")
    conversion_columns[2].metric("Quote conversion", f"{quote_conversion:,.1f}%")
    conversion_columns[3].metric("Value conversion", f"{value_conversion:,.1f}%")

    status_columns = st.columns(3)
    status_columns[0].metric("Green", int(traffic.eq("green").sum()))
    status_columns[1].metric("Amber", int(traffic.eq("amber").sum()))
    status_columns[2].metric("Red", int(traffic.eq("red").sum()))

    left, right = st.columns(2)
    by_user = (
        work.groupby("created_by_username", dropna=False)
        .agg(
            quotations=("quotation_key", "count"),
            quoted_value=("quoted_value", "sum"),
            annual_revenue=("annual_revenue", "sum"),
            average_spread=("spread_percent", "mean"),
            average_spread_per_hour=("spread_per_machine_hour", "mean"),
        )
        .reset_index()
        .sort_values("quoted_value", ascending=False)
    )
    with left:
        st.subheader("By user")
        st.dataframe(
            by_user,
            hide_index=True,
            width="stretch",
            column_config={
                "quoted_value": st.column_config.NumberColumn(format="£%.2f"),
                "average_spread": st.column_config.NumberColumn(format="%.1f%%"),
                "average_spread_per_hour": st.column_config.NumberColumn(format="£%.2f"),
            },
        )
    with right:
        st.subheader("Fulfilment")
        fulfilment = (
            work["fulfilment_type"].fillna("Not set").value_counts().rename_axis("type").reset_index(name="quotations")
        )
        st.bar_chart(fulfilment, x="type", y="quotations")

    recent = work.sort_values("created_at_utc", ascending=False).head(25).copy()
    recent["date"] = recent["created_at_utc"].dt.tz_convert("Europe/London").dt.strftime(
        "%d/%m/%Y"
    )
    recent["quote_reference"] = recent["quote_reference"].fillna("")
    st.subheader("Recent quotations")
    st.dataframe(
        recent[
            [
                "date",
                "quote_reference",
                "created_by_username",
                "customer_name",
                "item_code",
                "quoted_value",
                "spread_percent",
                "spread_per_machine_hour",
                "traffic_light_status",
                "esign_status",
            ]
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "quoted_value": st.column_config.NumberColumn(format="£%.2f"),
            "spread_percent": st.column_config.NumberColumn(format="%.1f%%"),
            "spread_per_machine_hour": st.column_config.NumberColumn(format="£%.2f"),
        },
    )
    render_admin_activity(
        repository,
        current_session_id,
        embedded=True,
    )


def render_required_password_change(
    repository: CsvRepository,
    user: Any,
) -> None:
    st.markdown("## Solidus")
    st.title("Choose a new password")
    st.write("Your administrator gave you a temporary password. Replace it before continuing.")
    with st.form("required_password_change"):
        password = st.text_input("New password", type="password")
        confirmation = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Save password", type="primary")
    if submitted:
        if len(password) < 10:
            st.error("Use at least 10 characters.")
        elif password != confirmation:
            st.error("The two passwords do not match.")
        else:
            try:
                repository.change_app_user_password(
                    user.username,
                    make_password_hash(password),
                    actor_username=user.username,
                )
            except RepositoryBusyError as exc:
                st.error(str(exc))
            else:
                _sign_out_local_session(
                    repository,
                    "Password changed. Sign in with your new password.",
                )
    st.stop()


def sync_multi_price_from_spread(index: int) -> None:
    bases = st.session_state.get("multi_item_price_bases", [])
    if index >= len(bases):
        return
    spread = float(st.session_state.get(f"multi_spread_{index}", 30.0) or 0)
    try:
        pricing = price_from_spread_percent(float(bases[index]), spread)
    except ValueError:
        return
    st.session_state[f"multi_price_{index}"] = pricing["selling_price_per_1000"]


def sync_multi_spread_from_price(index: int) -> None:
    bases = st.session_state.get("multi_item_price_bases", [])
    if index >= len(bases):
        return
    price = float(st.session_state.get(f"multi_price_{index}", 0.0) or 0)
    try:
        pricing = spread_percent_from_price(float(bases[index]), price)
    except ValueError:
        return
    st.session_state[f"multi_spread_{index}"] = pricing["spread_percent"]


def _overall_traffic_status(statuses: list[str]) -> str:
    normalised = [str(status).lower() for status in statuses]
    if "red" in normalised:
        return "red"
    if "amber" in normalised:
        return "amber"
    return "green"


def render_multi_item_quote(
    repository: CsvRepository,
    rate_table: HaulierRateTable,
    user_username: str,
    user_email: str,
    user_name: str,
    is_admin: bool,
) -> None:
    products = list(st.session_state.get("multi_item_products", []) or [])
    if len(products) < 2:
        st.session_state.pop("multi_item_mode", None)
        st.session_state.step = 0
        st.warning("Choose at least two existing products for a multi-item quotation.")
        st.rerun()

    heading, back = st.columns([4, 1])
    heading.subheader("Multi-item quotation")
    heading.caption(
        "Enter the shared customer and delivery details, then price each item."
    )
    if back.button("Change products", width="stretch"):
        for key in list(st.session_state):
            if key.startswith("multi_"):
                st.session_state.pop(key, None)
        st.session_state.pop("multi_item_mode", None)
        st.session_state.pop("multi_item_products", None)
        st.session_state.step = 0
        st.rerun()

    details_tab, delivery_tab, pricing_tab, save_tab = st.tabs(
        ["Quote details", "Delivery", "Price & approval", "Save & send"],
        key="multi_quote_tabs",
    )

    details_tab.markdown("#### Customer")
    customer_name = details_tab.text_input("Customer *", key="multi_customer_name")
    delivery_tab.markdown("#### Delivery")
    delivery_postcode = delivery_tab.text_input(
        "Delivery postcode *", key="multi_delivery_postcode"
    )
    fulfilment_type = details_tab.radio(
        "Fulfilment type",
        ["MTO", "MTC"],
        horizontal=True,
        key="multi_fulfilment_type",
    )
    delivery_mode = delivery_tab.radio(
        "Items will be",
        MULTI_DELIVERY_MODES,
        horizontal=True,
        key="multi_delivery_mode",
    )
    delivery_tab.caption(
        "Delivered together combines all item pallets before splitting the movement "
        "into trailers of up to 26 pallets. Delivered separately prices every item "
        "as its own movement."
    )

    agreement_term_months = 0
    pallets_per_delivery = 0
    holding_charge = 0.0
    if fulfilment_type == "MTC":
        term_col, calloff_col, holding_col = delivery_tab.columns(3)
        agreement_term_months = int(
            term_col.number_input(
                "Agreement term (months)",
                min_value=1,
                value=12,
                step=1,
                key="multi_agreement_term_months",
            )
        )
        pallets_per_delivery = int(
            calloff_col.number_input(
                "Minimum pallets per delivery",
                min_value=1,
                value=1,
                step=1,
                key="multi_pallets_per_delivery",
            )
        )
        holding_charge = float(
            holding_col.number_input(
                "Storage per pallet per week (£)",
                min_value=MIN_PALLET_HOLDING_CHARGE,
                value=MIN_PALLET_HOLDING_CHARGE,
                step=0.25,
                key="multi_holding_charge",
            )
        )

    collected = delivery_tab.checkbox(
        "Collected",
        key="multi_collected",
        help="Tick when the customer will collect. Otherwise the quotation is DAP.",
    )
    transport_service = "Next Day"
    transport_booking = "AM/PM"
    vendor_preference = "Highest available"
    if not collected:
        transport_columns = delivery_tab.columns(3 if is_admin else 2)
        transport_service = transport_columns[0].selectbox(
            "Service", ["Economy", "Next Day"], index=1, key="multi_service"
        )
        transport_booking = transport_columns[1].selectbox(
            "Booking", ["Standard", "AM/PM", "Timed"], index=1, key="multi_booking"
        )
        if is_admin:
            vendor_preference = transport_columns[2].selectbox(
                "Haulier",
                [
                    "Highest available",
                    "Cheapest available",
                    "Joda",
                    "McDowells",
                ],
                key="multi_vendor_preference",
            )

    with details_tab.expander("Customer considerations"):
        factor_columns = st.columns(4)
        factor_columns[0].checkbox(
            "Consistent Payer", key="multi_comex_consistent_payer"
        )
        factor_columns[1].checkbox(
            "Strategic Customer", key="multi_comex_strategic_customer"
        )
        factor_columns[2].checkbox(
            "Over Credit Limit", key="multi_comex_over_credit_limit"
        )
        factor_columns[3].checkbox(
            "Poor Payment History", key="multi_comex_poor_payment_history"
        )

    currency_col, rate_col = details_tab.columns(2)
    quote_currency = currency_col.selectbox(
        "Quotation currency", ["GBP", "EUR"], key="multi_quote_currency"
    )
    eur_per_gbp = 1.0
    eur_rate_date = ""
    eur_rate_source = ""
    if quote_currency == "EUR":
        try:
            eur_per_gbp, eur_rate_date = live_eur_per_gbp()
        except RuntimeError as exc:
            rate_col.error(str(exc))
        else:
            eur_rate_source = "ECB via Frankfurter"
            rate_col.metric("Live rate · EUR per GBP", f"{eur_per_gbp:.4f}")
    else:
        rate_col.caption("Prices will be shown in GBP.")

    symbol = currency_symbol(quote_currency)
    st.session_state.setdefault("multi_charge_description", "Forme / Stereo")
    st.session_state.setdefault(
        "multi_charge_amount", DEFAULT_TOOLING_CHARGE * len(products)
    )
    st.session_state.setdefault("multi_charge_foc", False)
    details_tab.markdown("#### Tooling")
    details_tab.caption(
        f"The default is {symbol}{DEFAULT_TOOLING_CHARGE:,.0f} per item "
        f"({symbol}{DEFAULT_TOOLING_CHARGE * len(products):,.0f} for this quote). "
        f"Each item's pricing base includes £{TOOLING_AMORTISATION_PER_1000:,.0f} "
        f"per 1,000, or £{FOC_TOOLING_AMORTISATION_PER_1000:,.0f} when tooling is FOC."
    )
    charge_columns = details_tab.columns([2, 1, 1])
    charge_description = charge_columns[0].text_input(
        "One-off charge description", key="multi_charge_description"
    )
    charge_amount = float(
        charge_columns[1].number_input(
            f"One-off charge total ({symbol})",
            min_value=0.0,
            step=25.0,
            key="multi_charge_amount",
            disabled=bool(st.session_state.multi_charge_foc),
        )
    )
    charge_foc = charge_columns[2].checkbox("FOC", key="multi_charge_foc")
    if charge_foc:
        charge_amount = 0.0

    details_tab.markdown("#### Items")
    line_inputs: list[dict[str, Any]] = []
    for index, product in enumerate(products):
        st.session_state.setdefault(
            f"multi_description_{index}", str(product.get("description", ""))
        )
        st.session_state.setdefault(f"multi_quantity_mode_{index}", "Units")
        st.session_state.setdefault(f"multi_quantity_units_{index}", 0.0)
        st.session_state.setdefault(f"multi_quantity_pallets_{index}", 0)
        st.session_state.setdefault(f"multi_annual_{index}", 0.0)
        with details_tab.container(border=True):
            st.markdown(f"**{product.get('item_code', '')}**")
            description = st.text_input(
                "Description",
                key=f"multi_description_{index}",
                label_visibility="collapsed",
            )
            mode_col, quantity_col = st.columns(2)
            quantity_mode = mode_col.selectbox(
                "Enter order quantity as",
                ["Units", "Pallets"],
                key=f"multi_quantity_mode_{index}",
            )
            per_pallet = float(product.get("pallet_quantity", 0) or 0)
            if quantity_mode == "Pallets":
                entered_pallets = int(
                    quantity_col.number_input(
                        "Order quantity (pallets)",
                        min_value=0,
                        step=1,
                        key=f"multi_quantity_pallets_{index}",
                    )
                )
                pallets = entered_pallets
                quantity = entered_pallets * per_pallet if per_pallet > 0 else 0.0
            else:
                quantity = float(
                    quantity_col.number_input(
                        "Order quantity (units)",
                        min_value=0.0,
                        step=1_000.0,
                        key=f"multi_quantity_units_{index}",
                    )
                )
                pallets = (
                    math.ceil(quantity / per_pallet)
                    if quantity > 0 and per_pallet > 0
                    else 0
                )
            annual_col, pallet_col, units_col = st.columns(3)
            annual = float(
                annual_col.number_input(
                    "Annual volume (units)",
                    min_value=0.0,
                    step=1_000.0,
                    key=f"multi_annual_{index}",
                )
            )
            pallet_col.metric("Equivalent pallets", f"{pallets:,}")
            units_col.metric("Units per pallet", f"{per_pallet:,.0f}")
            line_inputs.append(
                {
                    "description": description,
                    "quantity_input_mode": quantity_mode,
                    "order_quantity": quantity,
                    "annual_volume_units": annual,
                    "pallet_count": pallets,
                }
            )

    total_input_pallets = sum(int(line["pallet_count"]) for line in line_inputs)
    large_multi_order_confirmed = True
    if total_input_pallets > 26:
        details_tab.warning(
            f"This quotation is {total_input_pallets:,} pallets. Are you sure? "
            "Please check that an extra zero has not been entered."
        )
        large_multi_order_confirmed = details_tab.checkbox(
            f"Yes, I confirm the total order quantity is {total_input_pallets:,} pallets.",
            key=f"multi_confirm_large_order_{total_input_pallets}",
        )

    multi_input_basis = hashlib.sha256(
        json.dumps(
            {
                "customer_name": customer_name.strip(),
                "delivery_postcode": delivery_postcode.strip().upper(),
                "fulfilment_type": fulfilment_type,
                "delivery_mode": delivery_mode,
                "agreement_term_months": agreement_term_months,
                "pallets_per_delivery": pallets_per_delivery,
                "holding_charge": holding_charge,
                "collected": collected,
                "transport_service": transport_service,
                "transport_booking": transport_booking,
                "vendor_preference": vendor_preference,
                "quote_currency": quote_currency,
                "eur_per_gbp": eur_per_gbp,
                "additional_charge_foc": charge_foc,
                "customer_factors": {
                    key: bool(st.session_state.get(f"multi_{key}"))
                    for key in COMEX_FACTORS
                },
                "lines": line_inputs,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if (
        st.session_state.get("multi_item_input_basis")
        and st.session_state.get("multi_item_input_basis") != multi_input_basis
    ):
        st.session_state.multi_item_breakdowns = []
        st.session_state.multi_item_line_values = []
        st.session_state.multi_item_price_bases = []
        st.session_state.pop("multi_last_saved", None)
        st.session_state.pop("multi_saved_fingerprint", None)

    calculate_multi = pricing_tab.button(
        "Calculate item pricing",
        type="primary",
        width="stretch",
        disabled=(
            (quote_currency == "EUR" and eur_rate_source == "")
            or not large_multi_order_confirmed
        ),
    )
    if calculate_multi:
        errors: list[str] = []
        if not customer_name.strip():
            errors.append("Enter the customer name.")
        if not collected and not delivery_postcode.strip():
            errors.append("Enter the delivery postcode.")
        for product, line in zip(products, line_inputs):
            if line["order_quantity"] <= 0:
                errors.append(f"Enter an order quantity for {product.get('item_code', '')}.")
            if line["annual_volume_units"] <= 0:
                errors.append(f"Enter an annual volume for {product.get('item_code', '')}.")
            if line["pallet_count"] <= 0:
                errors.append(f"Check the pallet quantity for {product.get('item_code', '')}.")
        if errors:
            for error in errors:
                pricing_tab.error(error)
        else:
            try:
                transport = (
                    {
                        "total_cost": 0.0,
                        "line_costs": [0.0] * len(products),
                        "load_count": 0,
                        "delivery_count": 0,
                        "vendors": [],
                        "rate_zone": "",
                    }
                    if collected
                    else price_multi_item_transport(
                        rate_table,
                        pallet_counts=[line["pallet_count"] for line in line_inputs],
                        delivery_mode=delivery_mode,
                        postcode=delivery_postcode,
                        service=transport_service,
                        booking=transport_booking,
                        fulfilment_type=fulfilment_type,
                        pallets_per_delivery=pallets_per_delivery,
                        vendor_preference=vendor_preference,
                    )
                )
                breakdowns: list[dict[str, Any]] = []
                line_values: list[dict[str, Any]] = []
                for index, (product, line, transport_cost) in enumerate(
                    zip(products, line_inputs, transport["line_costs"])
                ):
                    values = {
                        **product,
                        **line,
                        "customer_name": customer_name.strip(),
                        "description": line["description"].strip(),
                        "delivery_postcode": delivery_postcode.strip().upper(),
                        "delivery_method": (
                            "Customer collection" if collected else "Haulier"
                        ),
                        "incoterm": "EXW" if collected else "DAP",
                        "transport_total": transport_cost,
                        "transport_service": transport_service,
                        "transport_booking": transport_booking,
                        "fulfilment_type": fulfilment_type,
                        "agreement_term_months": agreement_term_months,
                        "delivery_pallets_per_calloff": pallets_per_delivery,
                        "pallet_holding_charge_per_pallet_per_week": holding_charge,
                        "quote_currency": quote_currency,
                        "eur_per_gbp": eur_per_gbp,
                        "eur_rate_date": eur_rate_date,
                        "eur_rate_source": eur_rate_source,
                        "additional_charge_foc": charge_foc,
                        "comex_consistent_payer": bool(
                            st.session_state.get("multi_comex_consistent_payer")
                        ),
                        "comex_strategic_customer": bool(
                            st.session_state.get("multi_comex_strategic_customer")
                        ),
                        "comex_over_credit_limit": bool(
                            st.session_state.get("multi_comex_over_credit_limit")
                        ),
                        "comex_poor_payment_history": bool(
                            st.session_state.get("multi_comex_poor_payment_history")
                        ),
                    }
                    breakdown = calculate_cost(values)
                    breakdowns.append(breakdown)
                    line_values.append(values)
                    spread = float(st.session_state.get(f"multi_spread_{index}", 30.0) or 30.0)
                    st.session_state[f"multi_spread_{index}"] = spread
                    st.session_state[f"multi_price_{index}"] = price_from_spread_percent(
                        breakdown["pricing_base_per_1000"] * eur_per_gbp,
                        spread,
                    )["selling_price_per_1000"]
                st.session_state.multi_item_transport = transport
                st.session_state.multi_item_line_values = line_values
                st.session_state.multi_item_breakdowns = breakdowns
                st.session_state.multi_item_price_bases = [
                    breakdown["pricing_base_per_1000"] * eur_per_gbp
                    for breakdown in breakdowns
                ]
                st.session_state.multi_item_common = {
                    "customer_name": customer_name.strip(),
                    "delivery_postcode": delivery_postcode.strip().upper(),
                    "fulfilment_type": fulfilment_type,
                    "agreement_term_months": agreement_term_months,
                    "delivery_pallets_per_calloff": pallets_per_delivery,
                    "pallet_holding_charge_per_pallet_per_week": holding_charge,
                    "delivery_method": "Customer collection" if collected else "Haulier",
                    "incoterm": "EXW" if collected else "DAP",
                    "transport_service": transport_service,
                    "transport_booking": transport_booking,
                    "transport_vendor": ", ".join(sorted(set(transport["vendors"]))),
                    "transport_rate_zone": transport["rate_zone"],
                    "transport_total": transport["total_cost"],
                    "estimated_delivery_count": transport["delivery_count"],
                    "multi_delivery_mode": delivery_mode,
                    "quote_currency": quote_currency,
                    "eur_per_gbp": eur_per_gbp,
                    "eur_rate_date": eur_rate_date,
                    "eur_rate_source": eur_rate_source,
                }
                st.session_state.multi_item_input_basis = multi_input_basis
            except (TransportLookupError, ValueError) as exc:
                pricing_tab.error(str(exc))
            else:
                st.rerun()

    breakdowns = list(st.session_state.get("multi_item_breakdowns", []) or [])
    line_values = list(st.session_state.get("multi_item_line_values", []) or [])
    if len(breakdowns) != len(products) or len(line_values) != len(products):
        pricing_tab.info(
            "Complete the Quote details and Delivery tabs, then calculate the item pricing."
        )
        save_tab.info("Calculate the item pricing before saving the quotation.")
        return

    transport = dict(st.session_state.get("multi_item_transport", {}) or {})
    pricing_tab.info(
        f"Transport: {transport.get('delivery_count', 0):,} delivery event(s), "
        f"{transport.get('load_count', 0):,} trailer load(s), "
        f"£{float(transport.get('total_cost', 0) or 0):,.2f} total."
    )
    if is_admin and transport.get("vendors"):
        pricing_tab.caption(
            "Internal haulier selection: " + ", ".join(transport["vendors"])
        )

    line_records: list[dict[str, Any]] = []
    statuses: list[str] = []
    pricing_tab.markdown("#### Price and traffic light by item")
    for index, (values, breakdown) in enumerate(zip(line_values, breakdowns)):
        base = float(st.session_state.multi_item_price_bases[index])
        with pricing_tab.container(border=True):
            st.markdown(f"**{values.get('item_code', '')}** — {values.get('description', '')}")
            spread_col, price_col = st.columns(2)
            spread_col.number_input(
                "Spread (%)",
                min_value=-100_000.0,
                max_value=99.99,
                step=0.5,
                key=f"multi_spread_{index}",
                on_change=sync_multi_price_from_spread,
                args=(index,),
            )
            price_col.number_input(
                f"Selling price per 1,000 ({symbol})",
                min_value=0.01,
                step=1.0,
                key=f"multi_price_{index}",
                on_change=sync_multi_spread_from_price,
                args=(index,),
            )
            pricing = spread_percent_from_price(
                base, float(st.session_state[f"multi_price_{index}"])
            )
            material_pricing = price_from_spread_percent(
                float(breakdown.get("material_base_per_1000", 0) or 0),
                pricing["spread_percent"],
            )
            operational = operational_spread_metrics(
                material_pricing["spread_value_per_1000"],
                float(values["order_quantity"]),
                float(breakdown.get("machine_hours_per_1000", 0) or 0),
            )
            traffic = traffic_light_result(
                operational["spread_per_machine_hour"], pricing["spread_percent"]
            )
            statuses.append(traffic["status"])
            show_detail_cards(
                [
                    ("Selling price / item", f"{symbol}{pricing['selling_price_per_item']:.5f}"),
                    ("Spread", f"{pricing['spread_percent']:.2f}%"),
                    ("Spread / machine hour", f"£{operational['spread_per_machine_hour']:,.2f}"),
                    ("Traffic light", traffic["status"].upper()),
                ],
                container=pricing_tab,
            )
            if traffic["status"] == "red":
                pricing_tab.error(
                    "RED — Sales Director or delegated individual signature is required."
                )
            elif traffic["status"] == "amber":
                pricing_tab.warning("AMBER — review this item before continuing.")
            else:
                pricing_tab.success("GREEN — this item meets both commercial targets.")
            line_records.append(
                {
                    **values,
                    **breakdown,
                    **pricing,
                    **operational,
                    "material_spread_value_per_1000": material_pricing[
                        "spread_value_per_1000"
                    ],
                    "traffic_light_status": traffic["status"],
                    "traffic_light_reason": traffic["reason"],
                }
            )

    overall_status = _overall_traffic_status(statuses)
    if overall_status == "red":
        pricing_tab.error(
            "Overall route: RED. The salesperson's saved signature will be shown; "
            "the Sales Director or delegated individual will sign first because at "
            "least one item is red."
        )
    elif overall_status == "amber":
        pricing_tab.warning(
            "Overall route: AMBER. After the warning is acknowledged, the "
            "salesperson's saved signature will be applied, then the configured "
            "amber approver will sign before the Customer."
        )
    else:
        pricing_tab.success(
            "Overall route: GREEN. The salesperson's saved signature will be applied "
            "and only the Customer will be asked to sign."
        )

    amber_acknowledged = True
    if overall_status == "amber":
        amber_acknowledged = pricing_tab.checkbox(
            "I have reviewed the amber item(s).",
            key="multi_amber_acknowledged",
        )

    save_tab.markdown("#### Save and send")
    contact_col, email_col = save_tab.columns(2)
    customer_contact = contact_col.text_input(
        "Customer contact", key="multi_customer_contact"
    )
    customer_email = email_col.text_input("Customer email", key="multi_customer_email")
    customer_role = save_tab.text_input("Customer role", key="multi_customer_role")
    notes = save_tab.text_area("Quote notes", key="multi_quote_notes", height=90)

    quoted_value = sum(
        float(line["selling_price_per_item"]) * float(line["order_quantity"])
        for line in line_records
    ) + charge_amount
    annual_revenue = sum(
        float(line["selling_price_per_item"]) * float(line["annual_volume_units"])
        for line in line_records
    )
    show_detail_cards(
        [
            ("Items", len(line_records)),
            ("Total pallets", sum(int(line["pallet_count"]) for line in line_records)),
            ("Quote value", f"{symbol}{quoted_value:,.2f}"),
            ("Annual revenue", f"{symbol}{annual_revenue:,.2f}"),
        ],
        container=save_tab,
    )

    common = dict(st.session_state.get("multi_item_common", {}) or {})
    first = line_records[0]
    multi_esign_settings = configured_esign()
    approval_error = ""
    try:
        approval_recipient = commercial_approval_recipient(
            multi_esign_settings, overall_status
        )
    except ESignError as exc:
        approval_recipient = None
        approval_error = str(exc)
    if approval_error:
        save_tab.warning(approval_error)
    elif approval_recipient:
        save_tab.caption(
            f"{overall_status.upper()} route: {approval_recipient.role} "
            f"{approval_recipient.name} ({approval_recipient.email}) will sign "
            "before the Customer."
        )
    current_multi_record = {
        **first,
        **common,
        "item_code": "MULTI-ITEM",
        "source_item_code": "",
        "description": f"Multi-item quotation ({len(line_records)} items)",
        "is_multi_item_quote": True,
        "quote_items": line_records,
        "catalogue_product": False,
        "order_quantity": sum(float(line["order_quantity"]) for line in line_records),
        "annual_volume_units": sum(
            float(line["annual_volume_units"]) for line in line_records
        ),
        "pallet_count": sum(float(line["pallet_count"]) for line in line_records),
        "order_pallets": sum(float(line["pallet_count"]) for line in line_records),
        "traffic_light_status": overall_status,
        "traffic_light_reason": (
            "At least one item is red"
            if overall_status == "red"
            else "At least one item is amber"
            if overall_status == "amber"
            else "Every item is green"
        ),
        "quoted_value": quoted_value,
        "annual_revenue": annual_revenue,
        "customer_contact": customer_contact.strip(),
        "customer_email": customer_email.strip(),
        "customer_role": customer_role.strip(),
        "director_name": approval_recipient.name if approval_recipient else "",
        "director_email": approval_recipient.email if approval_recipient else "",
        "approval_recipient_name": (
            approval_recipient.name if approval_recipient else ""
        ),
        "approval_recipient_email": (
            approval_recipient.email if approval_recipient else ""
        ),
        "approval_recipient_role": (
            approval_recipient.role if approval_recipient else ""
        ),
        "approval_recipient_is_cover": bool(
            approval_recipient and approval_recipient.is_cover
        ),
        "notes": notes,
        "additional_charge_description": charge_description,
        "additional_charge_amount": charge_amount,
        "additional_charge_foc": charge_foc,
        "quote_reference": st.session_state.get("quote_reference", ""),
        "quote_number": st.session_state.get("quote_number", ""),
        "quote_revision": st.session_state.get("quote_revision", ""),
    }

    save_multi = save_tab.button(
        "Save multi-item quotation",
        type="primary",
        width="stretch",
        disabled=not amber_acknowledged,
    )
    if save_multi:
        try:
            current_multi_record.update(
                active_sales_rep_signature_metadata(
                    repository,
                    username=user_username,
                    name=user_name,
                )
            )
            saved = repository.save_costing(
                current_multi_record,
                user_username=user_username,
                user_email=user_email,
                user_name=user_name,
            )
        except RepositoryBusyError as exc:
            save_tab.error(str(exc))
        else:
            st.session_state.multi_last_saved = saved
            st.session_state.quote_reference = saved.get("quote_reference", "")
            st.session_state.quote_number = saved.get("quote_number", "")
            st.session_state.quote_revision = saved.get("quote_revision", "")
            st.session_state.multi_saved_fingerprint = saved_revision_fingerprint(saved)
            save_tab.success(
                f"Saved multi-item quotation {saved['quote_reference']}."
            )
            st.rerun()

    saved = st.session_state.get("multi_last_saved")
    current_fingerprint = saved_revision_fingerprint(current_multi_record)
    if (
        not saved
        or st.session_state.get("multi_saved_fingerprint") != current_fingerprint
    ):
        save_tab.warning(
            "Save this exact revision before downloading or sending it. "
            "If any item, quantity or price changes, save again."
        )
        return
    save_tab.markdown("#### Downloads")
    save_tab.download_button(
        "Customer quote PDF",
        data=quote_pdf(with_sales_rep_signature(repository, saved)),
        file_name=f"{saved.get('quote_reference') or 'multi-item-quote'}.pdf",
        mime="application/pdf",
        width="stretch",
    )
    with save_tab:
        render_esign_test(
            repository,
            saved,
            user_username=user_username,
            user_email=user_email,
            user_name=user_name,
        )


def render_workflow(
    repository: CsvRepository,
    rate_table: HaulierRateTable,
    user_username: str,
    user_email: str,
    user_name: str,
    can_create_new: bool,
    is_admin: bool,
) -> None:
    simple_mode = not can_create_new and not is_admin
    st.markdown(
        '<div class="brand-banner"><div><div class="brand-name">Solidus</div>'
        '<div class="brand-tagline">Your circular packaging partner</div></div>'
        '<div class="brand-tool">Spread Costing Tool</div></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Choose a product, enter the order, set the price and save the quotation."
        if simple_mode
        else "Select a product to start."
    )
    workflow_notice = st.session_state.pop("workflow_notice", None)
    if workflow_notice:
        st.success(workflow_notice)
    costing_in_progress = bool(
        st.session_state.get("draft")
        or st.session_state.get("multi_item_mode")
        or int(st.session_state.get("step", 0) or 0) > 0
    )
    if costing_in_progress:
        restart_note, restart = st.columns([4, 1])
        restart_note.caption(
            "Need a different product? Start again at any point without affecting "
            "saved quotations."
        )
        if restart.button(
            "↻ Start again",
            width="stretch",
            key="start_again",
        ):
            st.session_state.restart_confirmation = True
        if st.session_state.get("restart_confirmation"):
            with st.container(border=True):
                st.warning(
                    "Start again and clear the current item, customer, quantities "
                    "and prices? Saved quotations will remain in My costings."
                )
                confirm, cancel, _ = st.columns([1, 1, 3])
                if confirm.button(
                    "Yes, start again",
                    type="primary",
                    width="stretch",
                    key="confirm_start_again",
                ):
                    clear_costing_workflow()
                    st.rerun()
                if cancel.button(
                    "Keep costing",
                    width="stretch",
                    key="cancel_start_again",
                ):
                    st.session_state.pop("restart_confirmation", None)
                    st.rerun()
    if st.session_state.get("multi_item_mode"):
        render_multi_item_quote(
            repository,
            rate_table,
            user_username,
            user_email,
            user_name,
            is_admin,
        )
        return
    stage_navigation(simple_mode)
    if st.session_state.step == 0:
        render_select(repository, can_create_new, is_admin)
    elif st.session_state.step == 1:
        render_specification(repository, simple_mode)
    elif st.session_state.step == 2:
        render_costs(repository, rate_table, is_admin, simple_mode)
    elif st.session_state.step == 3:
        render_pricing(
            repository,
            user_username,
            user_email,
            user_name,
            is_admin,
            simple_mode,
        )
    else:
        render_save(
            repository,
            user_username,
            user_email,
            user_name,
            can_create_new,
            is_admin,
        )


def render_top_menu(
    repository: CsvRepository,
    user: Any,
    navigation: list[str],
) -> str:
    current = str(st.session_state.get("main_navigation", navigation[0]))
    if current not in navigation:
        current = navigation[0]
        st.session_state.main_navigation = current
    identity, menu = st.columns([5, 1], vertical_alignment="center")
    identity.caption(
        f"{current} · {user.name} · "
        + ("Neon storage" if repository.uses_database else "Local storage")
    )
    with menu:
        with st.popover("☰ Menu", use_container_width=True):
            st.caption(f"Signed in as {user.name} (@{user.username})")
            for option in navigation:
                if st.button(
                    option,
                    key=f"top_navigation_{option}",
                    type="primary" if option == current else "secondary",
                    width="stretch",
                ):
                    st.session_state.main_navigation = option
                    st.rerun()
            st.divider()
            st.caption(
                f"Signs out after {session_timeout_minutes()} minutes without activity."
            )
            sign_out_button(repository)
    return current


def main() -> None:
    data_dir = data_directory(PROJECT_ROOT)
    repository_source = PROJECT_ROOT / "src" / "repository.py"
    repository_code_version = hashlib.sha256(
        repository_source.read_bytes()
    ).hexdigest()
    repository = cached_repository(
        str(data_dir),
        configured_database_url(),
        repository_code_version,
    )
    user = require_user(repository)
    if user.must_change_password:
        render_required_password_change(repository, user)
    rate_table = cached_rate_table(
        str(repository.haulier_path),
        repository.reference_data_version()[-1],
    )
    st.session_state.setdefault("step", 0)

    draft = st.session_state.get("draft", {})
    if (
        not user.can_create_new
        and draft
        and not str(draft.get("source_item_code", "")).strip()
    ):
        st.session_state.pop("draft", None)
        reset_downstream()
        st.session_state.step = 0
        st.warning(
            "Your account can cost existing products only. Select a product to continue."
        )

    navigation = ["Costing workflow", "My costings", "My signature"]
    if user.can_view_history or user.is_admin:
        navigation.append("Team history")
    if user.is_admin:
        navigation.extend(["Dashboard", "Admin tools"])
    page = render_top_menu(repository, user, navigation)
    try:
        current_session_id = track_user_session(repository, user, page)
    except RepositoryBusyError as exc:
        st.error(str(exc))
        st.stop()
    session_heartbeat(repository, current_session_id)
    try:
        if page == "My costings":
            render_history(repository, user.email, user.is_admin)
        elif page == "My signature":
            render_my_signature(repository, user)
        elif page == "Team history":
            render_team_history(repository, user.is_admin)
        elif page == "Admin tools":
            render_admin_tools(repository, user)
        elif page == "Dashboard":
            render_admin_dashboard(repository, current_session_id)
        else:
            render_workflow(
                repository,
                rate_table,
                user.username,
                user.email,
                user.name,
                user.can_create_new,
                user.is_admin,
            )
    except RepositoryBusyError as exc:
        st.error(str(exc))


if __name__ == "__main__":
    main()
