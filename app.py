from __future__ import annotations

import html
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from src.auth import (
    authenticate_admin,
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
    annual_volume_band_for_units,
    calculate_cost,
    operational_spread_metrics,
    price_from_spread_percent,
    spread_percent_from_price,
    traffic_light_result,
    validate_details,
)
from src.exports import history_pdf, quote_pdf, sage_stock_import_csv
from src.esign import DropboxSignClient, ESignError, Signer
from src.repository import (
    CALCULATION_COLUMNS,
    COST_INPUT_COLUMNS,
    CsvRepository,
    RepositoryBusyError,
    SPECIFICATION_COLUMNS,
    data_directory,
)
from src.transport import HaulierRateTable, TransportLookupError


PROJECT_ROOT = Path(__file__).resolve().parent
STAGES = ["Product", "Order", "Costs", "Price", "Quote"]


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
    initial_sidebar_state="expanded",
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


def _sign_out_local_session(repository: CsvRepository, message: str) -> None:
    repository.end_session(st.session_state.get("app_session_id", ""))
    st.session_state.pop("authenticated_user", None)
    st.session_state.pop("app_session_id", None)
    st.session_state.pop("app_signed_in_at", None)
    st.session_state.pop("app_last_activity_at", None)
    st.session_state.pop("app_active_seconds", None)
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
        "material": "Solid board",
        "product_group": "Finished goods",
        "manufacturing_site": "101",
        "net_mass_kg": 0.0,
        "board_gsm": 1_000.0,
        "length_mm": 0.0,
        "width_mm": 0.0,
        "height_mm": 0.0,
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
        "material_cost_source": "",
        "machine_hours_per_1000": 0.0,
        "machine_time_source": "No BOM machine-time profile",
        "delivery_postcode": "",
        "delivery_method": "Haulier",
        "transport_service": "Economy",
        "transport_vendor_preference": "Cheapest available",
        "transport_vendor": "",
        "transport_booking": "Standard",
        "transport_rate_zone": "",
        "transport_manual_override": 0,
        "transport_total": 0.0,
        "spread_percent": 30.0,
        "source_item_code": "",
    }


def draft_number(key: str, fallback: float = 0.0) -> float:
    try:
        return float(st.session_state.draft.get(key, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback


def draft_flag(key: str) -> bool:
    value = st.session_state.draft.get(key, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def format_unit_price(value: Any) -> str:
    """Show every per-item amount to the agreed five decimal places."""
    try:
        return f"£{float(value):,.5f}"
    except (TypeError, ValueError):
        return "—"


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


def show_detail_cards(items: list[tuple[str, Any]]) -> None:
    """Show product facts without Streamlit metric's one-line truncation."""
    cards = "".join(
        '<div class="detail-card">'
        f'<div class="detail-label">{html.escape(str(label))}</div>'
        f'<div class="detail-value">{html.escape(str(value))}</div>'
        "</div>"
        for label, value in items
    )
    st.markdown(f'<div class="detail-grid">{cards}</div>', unsafe_allow_html=True)


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
    st.session_state.pop("customer_contact", None)
    st.session_state.pop("quote_notes", None)
    for key in list(st.session_state):
        if key.startswith("spec_board_") or key in {"spec_fsc", "board_lookup_notice"}:
            st.session_state.pop(key, None)


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
        return bool(pricing.get("traffic_override_approved"))
    if status == "amber":
        return bool(pricing.get("traffic_amber_acknowledged"))
    return True


def stage_navigation() -> None:
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

    mode = "Existing product"
    if can_create_new:
        mode = st.radio(
            "Costing route",
            ["Existing product", "New product"],
            horizontal=True,
        )

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
            st.session_state.draft = clean_record(draft)
            reset_downstream()
            navigate_to(1)
    else:
        with st.container(border=True):
            st.markdown("### Create a new product")
            st.write(
                "For products not already in the list. Have the product specification "
                "and board details ready."
            )
            if st.button("Create new product", type="primary", width="stretch"):
                st.session_state.draft = default_draft()
                reset_downstream()
                navigate_to(1)


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
    st.session_state.draft.update(match)
    price = float(match["board_price_per_tonne"])
    price_text = f"£{price:,.2f}/tonne" if price > 0 else "no current price"
    st.session_state.board_lookup_notice = (
        "success",
        f"{match['board_item_code']}: {match['board_width_mm']:,.0f} x "
        f"{match['board_length_mm']:,.0f} mm, {match['board_gsm']:,.0f} GSM, {price_text}.",
    )


def render_specification(repository: CsvRepository) -> None:
    st.subheader("Order and fulfilment")
    draft = st.session_state.draft
    existing_item = bool(str(draft.get("source_item_code", "")).strip())
    board_widget_defaults = {
        "spec_board_code": str(draft.get("board_code", "")),
        "spec_board_gsm": draft_number("board_gsm"),
        "spec_board_width": draft_number("board_width_mm"),
        "spec_board_length": draft_number("board_length_mm"),
        "spec_fsc": str(draft.get("fsc", "")),
    }
    for key, value in board_widget_defaults.items():
        st.session_state.setdefault(key, value)
    if existing_item:
        st.markdown(
            '<div class="status-card"><strong>'
            f"{str(draft.get('item_code', ''))}</strong> — "
            f"{str(draft.get('description', ''))}<br>"
            "Product details loaded. Open them below if anything needs changing.</div>",
            unsafe_allow_html=True,
        )

    expander_label = (
        "View or amend product specification"
        if existing_item
        else "Product specification *"
    )
    with st.expander(expander_label, expanded=not existing_item):
        left, right = st.columns(2)
        item_code = left.text_input(
            "Item code *", value=str(draft.get("item_code", ""))
        )
        description = right.text_input(
            "Description *", value=str(draft.get("description", ""))
        )

        col1, col2, col3 = st.columns(3)
        material_options = [
            "BOM-defined materials",
            "Solid board",
            "Corrugated",
            "Fibre",
            "Other",
        ]
        current_material = str(draft.get("material", "Solid board"))
        if current_material not in material_options:
            material_options.append(current_material)
        material = col1.selectbox(
            "Material *", material_options, index=material_options.index(current_material)
        )
        product_group = col2.text_input(
            "Product group", value=str(draft.get("product_group", "Finished goods"))
        )
        board_gsm = col3.number_input(
            "Grade / GSM *",
            min_value=0.0,
            step=25.0,
            key="spec_board_gsm",
        )

        col1, col2, col3 = st.columns(3)
        length_mm = col1.number_input(
            "Length (mm) *",
            min_value=0.0,
            value=draft_number("length_mm"),
            step=1.0,
        )
        width_mm = col2.number_input(
            "Width (mm) *",
            min_value=0.0,
            value=draft_number("width_mm"),
            step=1.0,
        )
        height_mm = col3.number_input(
            "Height (mm) *",
            min_value=0.0,
            value=draft_number("height_mm"),
            step=1.0,
        )

        col1, col2 = st.columns(2)
        pallet_quantity = col1.number_input(
            "Pallet quantity *",
            min_value=0,
            value=max(0, int(draft_number("pallet_quantity"))),
            step=1,
        )
        net_mass_kg = col2.number_input(
            "Net mass per item (kg)",
            min_value=0.0,
            value=draft_number("net_mass_kg"),
            step=0.0001,
            format="%.4f",
        )

        col1, col2, col3 = st.columns(3)
        board_width_mm = col1.number_input(
            "Board width / reel width (mm)",
            min_value=0.0,
            step=1.0,
            key="spec_board_width",
        )
        board_length_mm = col2.number_input(
            "Board length / chop (mm)",
            min_value=0.0,
            step=1.0,
            key="spec_board_length",
        )
        number_of_colours = col3.number_input(
            "Number of colours",
            min_value=0,
            value=max(0, int(draft_number("number_of_colours"))),
            step=1,
        )

        col1, col2 = st.columns(2)
        board_code = col1.text_input(
            "Board code", key="spec_board_code"
        )
        fsc = col2.text_input("FSC", key="spec_fsc")
        if not existing_item:
            st.button(
                "Fill board details from code",
                on_click=fill_board_details,
                args=(repository,),
            )
            notice = st.session_state.get("board_lookup_notice")
            if notice:
                getattr(st, notice[0])(notice[1])

    st.markdown("#### Required order details")
    left, right = st.columns(2)
    customer_name = left.text_input(
        "Customer *", value=str(draft.get("customer_name", ""))
    )
    delivery_postcode = right.text_input(
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
            min_value=0.0,
            value=max(0.0, pallet_holding_charge),
            step=1.0,
            help="Enter 0 if the rate is still to be agreed; the quotation will still flag that a charge may apply.",
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

    submitted = st.button("Save order details", type="primary")

    if submitted:
        updated = {
            **draft,
            "customer_name": customer_name.strip(),
            "item_code": item_code.strip().upper(),
            "description": description.strip(),
            "material": material,
            "product_group": product_group.strip(),
            "board_gsm": board_gsm,
            "length_mm": length_mm,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "pallet_quantity": pallet_quantity,
            "order_quantity": order_quantity,
            "net_mass_kg": net_mass_kg,
            "board_width_mm": board_width_mm,
            "board_length_mm": board_length_mm,
            "number_of_colours": number_of_colours,
            "board_code": board_code.strip(),
            "fsc": fsc.strip(),
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
) -> None:
    st.subheader("Material base and delivery")
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
        else:
            st.success("The BOM and board-price calculation is ready.")
    else:
        st.info(
            "Choose the board and, if needed, a comparable BOM for the other components."
        )
        board_catalog = repository.load_priced_board_catalog().copy()
        required_gsm = draft_number("board_gsm")
        if required_gsm > 0:
            matching_gsm = board_catalog[
                pd.to_numeric(board_catalog["effective_gsm"], errors="coerce").eq(
                    required_gsm
                )
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
        board_labels = {
            str(row["board_item_code"]): (
                f"{row['board_item_code']} — {row.get('board_item_name', '')}"
                + (
                    f" — {float(row['price_per_tonne']):,.0f} £/tonne"
                    if is_admin
                    else ""
                )
            )
            for _, row in board_catalog.iterrows()
        }
        board_options = ["", *board_labels]
        current_board = str(draft.get("board_item_code", ""))
        selected_board_code = st.selectbox(
            "Board item *",
            board_options,
            index=board_options.index(current_board)
            if current_board in board_options
            else 0,
            format_func=lambda value: board_labels.get(value, "Choose a board item"),
        )
        units_out = st.number_input(
            "Finished units out per board sheet *",
            min_value=0.01,
            value=max(0.01, draft_number("units_out", 1)),
            step=1.0,
            help="For example, enter 2 when one board sheet makes two finished items.",
        )

        templates = repository.load_current_items()
        templates = templates[
            pd.to_numeric(templates["bom_available"], errors="coerce").fillna(0).gt(0)
        ].sort_values("item_code")
        template_labels = {
            str(row["item_code"]): f"{row['item_code']} — {row.get('description', '')}"
            for _, row in templates.iterrows()
        }
        template_labels["__NONE__"] = "No other components required"
        template_options = [
            "",
            "__NONE__",
            *[code for code in template_labels if code != "__NONE__"],
        ]
        current_template = str(draft.get("component_template_item_code", ""))
        selected_template = st.selectbox(
            "Other-component template *",
            template_options,
            index=template_options.index(current_template)
            if current_template in template_options
            else 0,
            format_func=lambda value: template_labels.get(
                value, "Choose a comparable BOM or confirm none"
            ),
            help="Copies banding, pallets, layercards, wrap, adhesive and other non-board BOM components from a comparable item.",
        )
        if selected_board_code and selected_template:
            template_code = "" if selected_template == "__NONE__" else selected_template
            material_result = repository.new_item_material_breakdown(
                selected_board_code,
                units_out=units_out,
                component_template_item_code=template_code,
            )
            selected_board = board_catalog[
                board_catalog["board_item_code"].astype(str).eq(selected_board_code)
            ].iloc[0]

    st.markdown("#### Material setup")
    st.caption("Board and other components are calculated from the product data.")
    if material_result is not None:
        material_summary = material_result["summary"]
        material_lines = material_result["lines"]
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
        else:
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
    methods = ["Haulier", "Customer collection", "Included elsewhere"]
    current_method = str(draft.get("delivery_method", "Haulier"))
    delivery_method = st.selectbox(
        "Delivery method",
        methods,
        index=methods.index(current_method) if current_method in methods else 0,
    )

    service = str(draft.get("transport_service", "Economy"))
    booking = str(draft.get("transport_booking", "Standard"))
    vendor_preference = str(
        draft.get("transport_vendor_preference", "Cheapest available")
    )
    manual_override = bool(float(draft.get("transport_manual_override", 0) or 0))
    manual_transport_total = draft_number("transport_total")
    if delivery_method == "Haulier":
        col1, col2, col3 = st.columns(3)
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
        preferences = ["Cheapest available", "Joda", "McDowells"]
        vendor_preference = col3.selectbox(
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
        st.caption(
            "AM/PM, timed-booking and full-load charges are included automatically."
            if not is_admin
            else "AM/PM adds £7 per load; Timed adds £19 per load. McDowells adds £40 for each complete 26-pallet load."
        )

    calculate = st.button(
        "Calculate pricing base",
        type="primary",
        disabled=material_summary is None,
    )

    if calculate and material_summary is not None:
        updated = {
            **material_summary,
            "delivery_method": delivery_method,
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
                if vendor_preference == "Cheapest available":
                    selected_quote = quotes[0]
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
        else:
            st.success("Material and delivery have been calculated.")
        if st.button("Continue to pricing", type="primary"):
            navigate_to(3)


def sync_selling_from_spread() -> None:
    try:
        pricing = price_from_spread_percent(
            float(st.session_state.breakdown["pricing_base_per_1000"]),
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
            float(st.session_state.breakdown["pricing_base_per_1000"]),
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
) -> None:
    st.subheader("Set spread or selling price")
    breakdown = st.session_state.breakdown
    if is_admin:
        show_cost_breakdown(breakdown)
        show_admin_adjustment_detail(breakdown)
        st.info(
            "Spread = (selling price − pricing base) ÷ selling price. "
            "Change either figure to recalculate the other."
        )
    else:
        st.caption("Enter either the spread or selling price. The other figure will update.")

    pricing_base = float(breakdown["pricing_base_per_1000"])
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
        st.session_state.get("pricing_base_for_inputs") != pricing_base
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
        st.session_state.pricing_base_for_inputs = pricing_base
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
        "Selling price per 1,000 (£)",
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
            ("Selling price / 1,000", f"£{pricing['selling_price_per_1000']:,.2f}"),
            ("Selling price / item", format_unit_price(pricing["selling_price_per_item"])),
            ("Spread", f"{pricing['spread_percent']:,.2f}%"),
        ]
        if is_admin:
            pricing_cards.append(
                ("Spread value / 1,000", f"£{pricing['spread_value_per_1000']:,.2f}")
            )
        show_detail_cards(pricing_cards)
        st.markdown("#### Material-only operational spread")
        if pricing.get("total_machine_hours", 0) > 0:
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
        else:
            st.info(
                "No machine time is available for this BOM, so spread/hour cannot be calculated."
            )
        if pricing["spread_percent"] < 0:
            st.warning("The selected selling price produces a negative spread.")

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
                "selling price before continuing.</div>",
                unsafe_allow_html=True,
            )
        else:
            st.error(
                f"RED — this costing cannot continue without admin approval. "
                f"{traffic['reason'].capitalize()}."
            )

        override_approved = bool(pricing.get("traffic_override_approved"))
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

        if traffic["status"] == "red" and is_admin:
            if override_approved:
                st.success(
                    f"Override approved by {pricing.get('traffic_override_by_name', user_name)}. "
                    f"Reason: {pricing.get('traffic_override_reason', '')}"
                )
            else:
                override_reason = st.text_area(
                    "Reason for admin override *",
                    key="traffic_override_reason_input",
                    height=90,
                )
                if st.button(
                    "Approve red costing",
                    disabled=not override_reason.strip(),
                ):
                    pricing.update(
                        {
                            "traffic_override_approved": True,
                            "traffic_override_reason": override_reason.strip(),
                            "traffic_override_by_username": user_username,
                            "traffic_override_by_name": user_name,
                            "traffic_override_by_email": user_email,
                            "traffic_override_at_utc": _utc_now().isoformat(),
                            "traffic_override_basis": basis,
                        }
                    )
                    st.session_state.pricing = pricing
                    st.rerun()
        elif traffic["status"] == "red":
            if override_approved:
                st.success(
                    f"Override approved by {pricing.get('traffic_override_by_name', '')}. "
                    f"Reason: {pricing.get('traffic_override_reason', '')}"
                )
            else:
                st.write(
                    "An administrator can approve this costing below without signing "
                    "the current user out."
                )
                with st.form("red_admin_approval", clear_on_submit=True):
                    admin_username = st.text_input("Admin username")
                    admin_password = st.text_input("Admin password", type="password")
                    override_reason = st.text_area(
                        "Reason for admin override *", height=90
                    )
                    submitted = st.form_submit_button("Approve red costing")
                if submitted:
                    approving_admin = authenticate_admin(
                        admin_username,
                        admin_password,
                        repository,
                    )
                    if not override_reason.strip():
                        st.error("Enter a reason for the override.")
                    elif approving_admin is None:
                        st.error(
                            "The administrator username or password was not recognised."
                        )
                    else:
                        pricing.update(
                            {
                                "traffic_override_approved": True,
                                "traffic_override_reason": override_reason.strip(),
                                "traffic_override_by_username": approving_admin.username,
                                "traffic_override_by_name": approving_admin.name,
                                "traffic_override_by_email": approving_admin.email,
                                "traffic_override_at_utc": _utc_now().isoformat(),
                                "traffic_override_basis": basis,
                            }
                        )
                        st.session_state.pricing = pricing
                        st.rerun()

        can_continue = (
            traffic["status"] == "green"
            or (traffic["status"] == "amber" and amber_acknowledged)
            or (traffic["status"] == "red" and override_approved)
        )
        if st.button(
            "Continue to save and print",
            type="primary",
            disabled=not can_continue,
        ):
            navigate_to(4)


def current_record() -> dict[str, Any]:
    return {
        **st.session_state.draft,
        **st.session_state.breakdown,
        **st.session_state.pricing,
        "quote_reference": st.session_state.get("quote_reference", ""),
        "customer_contact": st.session_state.get("customer_contact", ""),
        "customer_email": st.session_state.get("customer_email", ""),
        "director_name": st.session_state.get("director_name", ""),
        "director_email": st.session_state.get("director_email", ""),
        "notes": st.session_state.get("quote_notes", ""),
    }


SAVED_REVISION_FIELDS = [
    *SPECIFICATION_COLUMNS,
    *COST_INPUT_COLUMNS,
    *CALCULATION_COLUMNS,
    "source_item_code",
    "quote_reference",
    "customer_contact",
    "customer_email",
    "director_name",
    "director_email",
    "notes",
]


def valid_email(value: str) -> bool:
    value = str(value or "").strip()
    return "@" in value and "." in value.rsplit("@", 1)[-1]


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
    st.markdown("#### Test e-signature")
    st.caption(
        "This route is locked to Dropbox Sign test mode. It sends real test emails, "
        "but the watermarked document is not legally binding."
    )
    customer_name = str(saved.get("customer_contact") or saved.get("customer_name") or "").strip()
    customer_email = str(saved.get("customer_email", "") or "").strip()
    director_name = str(saved.get("director_name", "") or "").strip()
    director_email = str(saved.get("director_email", "") or "").strip()
    request_id = str(saved.get("esign_request_id", "") or "").strip()
    status = str(saved.get("esign_status", "") or "").strip()

    if request_id:
        st.info(f"Dropbox Sign test status: **{status or 'sent'}**")
        for signer in saved.get("esign_signers", []) or []:
            st.write(
                f"{signer.get('name') or signer.get('email')}: "
                f"{str(signer.get('status', 'unknown')).replace('_', ' ')}"
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
    if not valid_email(customer_email):
        problems.append("customer email")
    if not director_name:
        problems.append("Sales Director name")
    if not valid_email(director_email):
        problems.append("Sales Director email")
    if customer_email.casefold() == director_email.casefold() and customer_email:
        problems.append("different Director and Customer email addresses")
    if problems:
        st.warning("Save this revision with " + ", ".join(problems) + " before sending it.")
        return

    approved = st.checkbox(
        "I approve this exact saved test quotation and want the Director and Customer emails sent."
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
        }
        try:
            approved_record = repository.update_costing_esign(
                str(saved.get("costing_id", "")), approval, owner_email=user_email
            )
        except RepositoryBusyError as exc:
            st.error(str(exc))
            return
        try:
            result = DropboxSignClient(api_key).send_test_request(
                quote_pdf(approved_record, esign_tags=True),
                title=f"Solidus quotation {saved.get('quote_reference') or saved.get('costing_id')}",
                subject=f"Test signature request: Solidus quotation {saved.get('quote_reference') or ''}",
                message=(
                    "This is a non-binding test of the Solidus quotation signing process. "
                    "The Sales Director is asked to sign first, followed by the Customer."
                ),
                director=Signer(director_name, director_email, 0),
                customer=Signer(customer_name, customer_email, 1),
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
            st.success("Test request sent. The Sales Director should receive the first email.")
            st.rerun()


def saved_revision_fingerprint(record: dict[str, Any]) -> str:
    """Identify the exact quoteable content represented by a saved revision."""
    normalised: dict[str, Any] = {}
    for field in SAVED_REVISION_FIELDS:
        value = record.get(field, "")
        if pd.isna(value):
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
    draft = st.session_state.draft
    st.session_state.setdefault(
        "quote_reference",
        f"Q-{datetime.now():%Y%m%d}-{str(draft['item_code'])[-6:]}-"
        f"{uuid.uuid4().hex[:4].upper()}",
    )
    st.session_state.setdefault("customer_contact", "")
    esign_settings = configured_esign()
    st.session_state.setdefault("customer_email", "")
    # The Director is a centrally managed recipient, not a per-quotation choice.
    st.session_state.director_name = str(
        esign_settings.get("director_name", "") or ""
    ).strip()
    st.session_state.director_email = str(
        esign_settings.get("director_email", "") or ""
    ).strip()
    st.session_state.setdefault("quote_notes", "")

    left, right = st.columns(2)
    left.text_input("Quote reference", key="quote_reference")
    right.text_input("Customer contact", key="customer_contact")
    if str(esign_settings.get("api_key", "") or "").strip():
        st.caption("Test e-sign recipients")
        st.text_input("Customer email", key="customer_email")
        if st.session_state.director_name and valid_email(
            st.session_state.director_email
        ):
            st.caption(
                "Sales Director: "
                f"{st.session_state.director_name} "
                f"({st.session_state.director_email}). "
                "This is set centrally by an administrator."
            )
        else:
            st.warning(
                "An administrator must set the Sales Director name and email "
                "in Streamlit Secrets before e-signing can be used."
            )
    quote_notes = st.text_area("Quote notes", key="quote_notes", height=100)

    record = current_record()
    record["notes"] = quote_notes
    summary_cards = [
        ("Item", record["item_code"]),
        ("Quantity", f"{float(record['order_quantity']):,.0f}"),
        ("Fulfilment", record.get("fulfilment_type", "MTO")),
        ("Sell / 1,000", f"£{record['selling_price_per_1000']:,.2f}"),
    ]
    if is_admin:
        summary_cards.insert(
            3,
            ("Pricing base / 1,000", f"£{record['pricing_base_per_1000']:,.2f}"),
        )
    show_detail_cards(summary_cards)

    if st.button("Save as a new revision", type="primary", width="stretch"):
        record["source_item_code"] = draft.get("source_item_code", "")
        try:
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

    export_record = dict(saved)
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

    st.session_state.draft = clean_record(draft)
    reset_downstream()
    st.session_state.customer_contact = str(record.get("customer_contact", "") or "")
    st.session_state.customer_email = str(record.get("customer_email", "") or "")
    st.session_state.director_name = str(record.get("director_name", "") or "")
    st.session_state.director_email = str(record.get("director_email", "") or "")
    st.session_state.quote_notes = str(record.get("notes", "") or "")
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
    st.dataframe(
        filtered[visible_columns],
        hide_index=True,
        width="stretch",
        column_config={
            "created_by_username": st.column_config.TextColumn("Username"),
            "pricing_base_per_1000": st.column_config.NumberColumn(format="£%.2f"),
            "selling_price_per_1000": st.column_config.NumberColumn(format="£%.2f"),
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
            f"{row.get('customer_name', '')} · {str(row['created_at_utc'])[:16]}"
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
                    format_unit_price(selected_record.get("selling_price_per_item")),
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
        columns = st.columns(2)
        columns[0].download_button(
            "Download CSV",
            data=export_history.to_csv(index=False).encode("utf-8-sig"),
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
        "selling_price_per_1000",
        "spread_percent",
        "spread_per_machine_hour",
        "traffic_light_status",
        "traffic_override_by_username",
        "traffic_override_reason",
        "esign_status",
        "costing_id",
    ]
    st.dataframe(
        filtered[visible_columns],
        hide_index=True,
        width="stretch",
        column_config={
            "created_by_username": st.column_config.TextColumn("Username"),
            "created_by_name": st.column_config.TextColumn("Name"),
            "selling_price_per_1000": st.column_config.NumberColumn(format="£%.2f"),
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
        columns = st.columns(2)
        columns[0].download_button(
            "Download CSV",
            data=export_history.to_csv(index=False).encode("utf-8-sig"),
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
            "last_login_at_utc": st.column_config.DatetimeColumn(
                "Last login", format="DD/MM/YYYY HH:mm"
            ),
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
    with st.form("edit_database_user"):
        edited_name = st.text_input("Name", value=str(selected_row.get("name", "")))
        edited_email = st.text_input("Email", value=str(selected_row.get("email", "")))
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
                st.success(f"Updated @{selected}.")
                st.rerun()

    audit = repository.load_app_audit_log()
    if not audit.empty:
        with st.expander("Recent user changes"):
            st.dataframe(audit, hide_index=True, width="stretch")


def render_admin_activity(
    repository: CsvRepository,
    current_session_id: str,
    current_username: str,
) -> None:
    st.header("User activity")
    st.caption(
        "Live sessions and saved-costing activity. Times are approximate and use UTC."
    )
    if repository.uses_database:
        st.success("Saved costings and session activity are using Neon.")
        render_user_management(repository, current_username)
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
    sessions["signed_in"] = sessions["_signed_in_at_utc"].dt.strftime(
        "%Y-%m-%d %H:%M"
    )
    sessions["last_activity"] = sessions["_last_activity_utc"].dt.strftime(
        "%Y-%m-%d %H:%M"
    )

    today = now.date()
    signed_today = sessions["_signed_in_at_utc"].dt.date.eq(today)
    if history.empty:
        saved_today = 0
    else:
        saved_times = pd.to_datetime(history["created_at_utc"], utc=True, errors="coerce")
        saved_today = int(saved_times.dt.date.eq(today).sum())
    show_detail_cards(
        [
            ("Online now", str(int(sessions["status"].eq("Online").sum()))),
            ("Sessions today", str(int(signed_today.sum()))),
            ("Costings saved today", str(saved_today)),
            ("Saved costings", str(len(history))),
        ]
    )

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

    st.subheader("Saved work by user")
    if history.empty:
        st.info("No costings have been saved yet.")
    else:
        work = history.copy()
        work["created_by_username"] = (
            work["created_by_username"].fillna("").astype(str)
        )
        work["order_quantity"] = pd.to_numeric(
            work["order_quantity"], errors="coerce"
        ).fillna(0)
        summary = (
            work.groupby("created_by_username", as_index=False)
            .agg(
                saved_costings=("costing_id", "count"),
                products=("item_code", "nunique"),
                customers=("customer_name", "nunique"),
                quoted_units=("order_quantity", "sum"),
                last_saved=("created_at_utc", "max"),
            )
            .sort_values("last_saved", ascending=False)
        )
        st.dataframe(summary, hide_index=True, width="stretch")

    st.caption(
        "Active time counts short gaps between actions, not simply an open browser tab. "
        "With Neon connected, this activity is retained when Streamlit restarts."
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
                st.session_state.authenticated_user["must_change_password"] = False
                st.success("Password changed.")
                st.rerun()
    st.stop()


def render_workflow(
    repository: CsvRepository,
    rate_table: HaulierRateTable,
    user_username: str,
    user_email: str,
    user_name: str,
    can_create_new: bool,
    is_admin: bool,
) -> None:
    st.markdown(
        '<div class="brand-banner"><div><div class="brand-name">Solidus</div>'
        '<div class="brand-tagline">Your circular packaging partner</div></div>'
        '<div class="brand-tool">Spread Costing Tool</div></div>',
        unsafe_allow_html=True,
    )
    st.caption("Select a product to start.")
    workflow_notice = st.session_state.pop("workflow_notice", None)
    if workflow_notice:
        st.success(workflow_notice)
    stage_navigation()
    if st.session_state.step == 0:
        render_select(repository, can_create_new, is_admin)
    elif st.session_state.step == 1:
        render_specification(repository)
    elif st.session_state.step == 2:
        render_costs(repository, rate_table, is_admin)
    elif st.session_state.step == 3:
        render_pricing(
            repository,
            user_username,
            user_email,
            user_name,
            is_admin,
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

    st.sidebar.markdown("## Solidus")
    st.sidebar.caption("Your circular packaging partner")
    st.sidebar.markdown("### Spread Costing Tool")
    st.sidebar.caption(f"Signed in as {user.name} (@{user.username})")
    st.sidebar.caption(
        "Access: "
        + ("existing and new products" if user.can_create_new else "existing products")
    )
    st.sidebar.caption(
        "Storage: Neon database" if repository.uses_database else "Storage: local CSV"
    )
    navigation = ["Costing workflow", "My costings"]
    if user.can_view_history or user.is_admin:
        navigation.append("Team history")
    if user.is_admin:
        navigation.append("User activity")
    page = st.sidebar.radio(
        "Navigation",
        navigation,
        key="main_navigation",
    )
    try:
        current_session_id = track_user_session(repository, user, page)
    except RepositoryBusyError as exc:
        st.error(str(exc))
        st.stop()
    session_heartbeat(repository, current_session_id)
    st.sidebar.divider()
    st.sidebar.caption(
        f"Signs out after {session_timeout_minutes()} minutes without activity."
    )
    sign_out_button(repository)

    try:
        if page == "My costings":
            render_history(repository, user.email, user.is_admin)
        elif page == "Team history":
            render_team_history(repository, user.is_admin)
        elif page == "User activity":
            render_admin_activity(
                repository,
                current_session_id,
                user.username,
            )
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
