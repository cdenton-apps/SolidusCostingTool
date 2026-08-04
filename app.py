from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from src.auth import require_user, sign_out_button
from src.calculations import (
    calculate_cost,
    price_from_spread,
    spread_from_price,
    validate_details,
)
from src.exports import history_pdf, quote_pdf, sage_stock_import_csv
from src.repository import (
    COST_INPUT_COLUMNS,
    CsvRepository,
    SPECIFICATION_COLUMNS,
    data_directory,
)
from src.transport import HaulierRateTable, TransportLookupError


PROJECT_ROOT = Path(__file__).resolve().parent
STAGES = ["1 · Select", "2 · Specification", "3 · Costs", "4 · Price", "5 · Save"]

st.set_page_config(
    page_title="Solidus Costing Tool",
    page_icon="♻️",
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
    .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1280px; }
    h1, h2, h3 { color: var(--solidus-ink); letter-spacing: -0.025em; }
    div[data-testid="stMetric"] { background: #ffffff; border: 1px solid var(--solidus-grey);
        border-top: 5px solid var(--solidus-yellow); border-radius: 12px; padding: 12px 16px; }
    div[data-testid="stForm"] { border-color: var(--solidus-grey); border-radius: 14px; }
    .status-card { padding: 1rem 1.1rem; border-radius: 12px; background: var(--solidus-mist);
        border-left: 5px solid var(--solidus-yellow); margin: .5rem 0 1rem; }
    .small-note { color: #4a5050; font-size: .9rem; }
    .brand-banner { display: flex; align-items: center; justify-content: space-between;
        gap: 1rem; padding: .8rem 1rem; margin: 0 0 1.1rem; background: var(--solidus-mist);
        border-radius: 14px; border-right: 12px solid var(--solidus-yellow); }
    .brand-name { font-size: 2.2rem; line-height: 1; font-weight: 800; letter-spacing: -.06em; }
    .brand-tagline { font-size: .9rem; font-weight: 700; margin-top: .35rem; }
    .brand-tool { font-size: 1rem; font-weight: 700; background: var(--solidus-yellow);
        padding: .55rem .8rem; border-radius: 999px; white-space: nowrap; }
    .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
        background: var(--solidus-yellow); color: var(--solidus-ink); border-color: var(--solidus-gold); }
    .stButton > button[kind="primary"]:hover { background: var(--solidus-gold); color: var(--solidus-ink); }
    div[data-testid="stProgressBar"] > div > div { background-color: var(--solidus-yellow); }
    @media (max-width: 700px) { .block-container { padding: 1rem; } }
    </style>
    """,
    unsafe_allow_html=True,
)


def clean_record(values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        cleaned[key] = "" if pd.isna(value) else value
    return cleaned


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
        "order_quantity": 0,
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
        "manual_adjustment_per_1000": 0.0,
        "fixed_tooling_cost": 0.0,
        "delivery_postcode": "",
        "delivery_method": "Haulier",
        "transport_service": "Economy",
        "transport_vendor_preference": "Cheapest available",
        "transport_vendor": "",
        "transport_booking": "Standard",
        "transport_rate_zone": "",
        "transport_manual_override": 0,
        "transport_total": 0.0,
        "target_spread_per_tonne": 0.0,
        "source_item_code": "",
    }


def draft_number(key: str, fallback: float = 0.0) -> float:
    try:
        return float(st.session_state.draft.get(key, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback


def reset_downstream() -> None:
    st.session_state.pop("breakdown", None)
    st.session_state.pop("pricing", None)
    st.session_state.pop("transport_quotes", None)
    st.session_state.pop("material_lines", None)
    st.session_state.pop("last_saved", None)


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
    return bool(st.session_state.get("pricing"))


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
    metric_columns = st.columns(4)
    metric_columns[0].metric(
        "Pricing base / 1,000", f"£{breakdown['pricing_base_per_1000']:,.2f}"
    )
    metric_columns[1].metric(
        "Pricing base / item", f"£{breakdown['pricing_base_per_item']:,.4f}"
    )
    metric_columns[2].metric("Pallets", f"{breakdown['pallet_count']:,.0f}")
    metric_columns[3].metric(
        "Net kg / 1,000", f"{breakdown['net_weight_kg_per_1000']:,.2f}"
    )
    rows = [
        ("Calculated materials", breakdown["materials_cost_per_1000"]),
        ("Commercial adjustment", breakdown["manual_adjustment_per_1000"]),
        ("Tooling pass-through", breakdown["tooling_cost_per_1000"]),
        ("Delivery pass-through", breakdown["transport_cost_per_1000"]),
    ]
    table = pd.DataFrame(rows, columns=["Cost element", "Cost per 1,000"])
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "Cost per 1,000": st.column_config.NumberColumn(format="£%.2f")
        },
    )


def render_select(repository: CsvRepository) -> None:
    st.subheader("Start a costing")
    mode = st.radio(
        "What would you like to cost?",
        ["Existing item", "New item"],
        horizontal=True,
    )

    if mode == "Existing item":
        catalog = repository.load_catalog()
        if catalog.empty:
            st.info("No current-item feed has been loaded yet. Start a new item instead.")
            return
        catalog = catalog.sort_values("item_code").reset_index(drop=True)
        labels = {
            index: f"{row['item_code']} — {row.get('description', '')}"
            for index, row in catalog.iterrows()
        }
        selected_index = st.selectbox(
            "Find an item",
            options=list(labels),
            format_func=labels.get,
            placeholder="Search by item code or description",
        )
        selected = clean_record(catalog.loc[selected_index].to_dict())
        selected_material_total = float(
            selected.get("materials_cost_per_1000", 0) or 0
        )
        columns = st.columns(5)
        columns[0].metric("Product group", str(selected.get("product_group", "—")))
        columns[1].metric("GSM", f"{float(selected.get('board_gsm', 0) or 0):,.0f}")
        columns[2].metric(
            "Pallet quantity", f"{float(selected.get('pallet_quantity', 0) or 0):,.0f}"
        )
        columns[3].metric(
            "Calculated material / 1,000",
            f"£{selected_material_total:,.2f}"
            if float(selected.get("bom_available", 0) or 0)
            else "No BOM",
        )
        columns[4].metric("Source", str(selected.get("source_type", "Feed")))
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
        if not material_lines.empty:
            with st.expander(
                f"View automatic material calculation ({len(material_lines)} components)"
            ):
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
        st.caption(
            "You can use this item as the basis of a new costing and alter any field. Saving always creates a new revision."
        )
        if st.button("Use this item", type="primary"):
            draft = default_draft()
            draft.update(
                {key: selected.get(key, draft.get(key)) for key in SPECIFICATION_COLUMNS}
            )
            draft.update(
                {key: selected.get(key, draft.get(key)) for key in COST_INPUT_COLUMNS}
            )
            if "target_spread_per_tonne" in selected:
                draft["target_spread_per_tonne"] = selected[
                    "target_spread_per_tonne"
                ]
            draft["source_item_code"] = selected.get("item_code", "")
            st.session_state.draft = clean_record(draft)
            reset_downstream()
            navigate_to(1)
    else:
        st.markdown(
            '<div class="status-card"><strong>New item</strong><br>'
            "Enter the technical specification, cost it, then create a quote and an indicative Sage import row.</div>",
            unsafe_allow_html=True,
        )
        if st.button("Create a new costing", type="primary"):
            st.session_state.draft = default_draft()
            reset_downstream()
            navigate_to(1)


def render_specification() -> None:
    st.subheader("Item specification")
    draft = st.session_state.draft
    with st.form("specification_form"):
        left, right = st.columns(2)
        customer_name = left.text_input("Customer *", value=str(draft.get("customer_name", "")))
        item_code = right.text_input("Item code *", value=str(draft.get("item_code", "")))
        description = st.text_input("Description *", value=str(draft.get("description", "")))

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
            value=draft_number("board_gsm"),
            step=25.0,
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

        col1, col2, col3 = st.columns(3)
        pallet_quantity = col1.number_input(
            "Pallet quantity *",
            min_value=0,
            value=max(0, int(draft_number("pallet_quantity"))),
            step=1,
        )
        order_quantity = col2.number_input(
            "Order quantity *",
            min_value=0,
            value=max(0, int(draft_number("order_quantity"))),
            step=1_000,
        )
        net_mass_kg = col3.number_input(
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
            value=draft_number("board_width_mm"),
            step=1.0,
        )
        board_length_mm = col2.number_input(
            "Board length / chop (mm)",
            min_value=0.0,
            value=draft_number("board_length_mm"),
            step=1.0,
        )
        number_of_colours = col3.number_input(
            "Number of colours",
            min_value=0,
            value=max(0, int(draft_number("number_of_colours"))),
            step=1,
        )

        col1, col2, col3 = st.columns(3)
        board_code = col1.text_input("Board code", value=str(draft.get("board_code", "")))
        fsc = col2.text_input("FSC", value=str(draft.get("fsc", "")))
        delivery_postcode = col3.text_input(
            "Delivery postcode *", value=str(draft.get("delivery_postcode", ""))
        )
        submitted = st.form_submit_button("Save specification", type="primary")

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
            "delivery_postcode": delivery_postcode.strip().upper(),
        }
        errors = validate_details(updated)
        if errors:
            for error in errors:
                st.error(error)
        else:
            st.session_state.draft = updated
            reset_downstream()
            st.success("Specification complete.")
            navigate_to(2)


def render_costs(repository: CsvRepository, rate_table: HaulierRateTable) -> None:
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
        st.success(
            f"Materials calculated automatically from the BOM and board data: £{imported_total:,.2f} per 1,000."
        )
    else:
        st.info(
            "This item has no BOM. Choose a priced board and a component template; the app will calculate the material value."
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
                f"{row['board_item_code']} — {row.get('board_item_name', '')} — "
                f"{float(row['price_per_tonne']):,.0f} £/tonne"
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

    st.markdown("#### Included pricing components")
    st.caption(
        "Board is priced from the April 2026 mill list wherever an unambiguous match exists. Other components come from the BOM. Machine and labour are excluded."
    )
    if material_result is not None:
        material_summary = material_result["summary"]
        material_lines = material_result["lines"]
        metrics = st.columns(4)
        metrics[0].metric(
            "Board / 1,000", f"£{float(material_summary['board_cost_per_1000']):,.2f}"
        )
        metrics[1].metric(
            "Other components / 1,000",
            f"£{float(material_summary['other_components_cost_per_1000']):,.2f}",
        )
        metrics[2].metric(
            "Total materials / 1,000",
            f"£{float(material_summary['materials_cost_per_1000']):,.2f}",
        )
        metrics[3].metric(
            "Board rate",
            f"£{float(material_summary['board_price_per_tonne']):,.2f} / tonne",
        )
        st.caption(
            f"{material_summary.get('board_article_code') or material_summary.get('board_item_code', 'Board')} · "
            f"{material_summary.get('board_price_source', '')}"
        )
        if not material_lines.empty:
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

    col1, col2 = st.columns(2)
    manual_adjustment = col2.number_input(
        "Commercial adjustment per 1,000 (£)",
        value=draft_number("manual_adjustment_per_1000"),
        step=1.0,
        help="Use a negative value for a credit or reduction.",
    )
    fixed_tooling = col1.number_input(
        "Tooling pass-through for this order (£)",
        min_value=0.0,
        value=draft_number("fixed_tooling_cost"),
        step=10.0,
    )

    st.markdown("#### Transport")
    estimated_pallets = math.ceil(
        draft_number("order_quantity") / draft_number("pallet_quantity", 1)
    )
    st.caption(
        f"Order quantity requires {estimated_pallets:,} pallet(s). Rates cover 1–26 pallets per load; larger orders are split into additional loads."
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
        manual_override = st.checkbox(
            "Use a manual transport total",
            value=manual_override,
        )
        if manual_override:
            manual_transport_total = st.number_input(
                "Manual transport total (£)",
                min_value=0.0,
                value=manual_transport_total,
                step=1.0,
            )
        st.caption(
            "AM/PM adds £7 per load; Timed adds £19 per load. McDowells adds £40 for each complete 26-pallet load."
        )

    calculate = st.button(
        "Calculate pricing base",
        type="primary",
        disabled=material_summary is None,
    )

    if calculate and material_summary is not None:
        updated = {
            **material_summary,
            "manual_adjustment_per_1000": manual_adjustment,
            "fixed_tooling_cost": fixed_tooling,
            "delivery_method": delivery_method,
            "transport_service": service,
            "transport_booking": booking,
            "transport_vendor_preference": vendor_preference,
            "transport_manual_override": int(manual_override),
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
                quotes = rate_table.quote_options(
                    postcode=str(draft["delivery_postcode"]),
                    pallet_count=estimated_pallets,
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
        if quotes:
            quote_frame = pd.DataFrame(quotes).rename(
                columns={
                    "vendor": "Haulier",
                    "rate_zone": "Rate zone",
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
                f"£{float(draft.get('transport_total', 0)):,.2f}."
            )
        show_cost_breakdown(st.session_state.breakdown)
        if st.button("Continue to pricing", type="primary"):
            navigate_to(3)


def render_pricing() -> None:
    st.subheader("Set spread or selling price")
    breakdown = st.session_state.breakdown
    show_cost_breakdown(breakdown)
    st.info(
        "Spread is measured in £ per tonne. Selling price = pricing base + (spread × net tonnes per 1,000)."
    )
    basis = st.radio(
        "Which value do you want to control?",
        ["Target spread", "Selling price"],
        horizontal=True,
    )
    with st.form("pricing_form"):
        if basis == "Target spread":
            value = st.number_input(
                "Target spread (£/tonne)",
                min_value=0.0,
                value=float(
                    st.session_state.get("pricing", {}).get(
                        "target_spread_per_tonne",
                        draft_number("target_spread_per_tonne", 0),
                    )
                ),
                step=10.0,
            )
        else:
            suggested = price_from_spread(
                breakdown["pricing_base_per_1000"],
                breakdown["net_weight_kg_per_1000"],
                draft_number("target_spread_per_tonne", 0),
            )["selling_price_per_1000"]
            value = st.number_input(
                "Selling price per 1,000 (£)",
                min_value=0.01,
                value=float(
                    st.session_state.get("pricing", {}).get(
                        "selling_price_per_1000", suggested
                    )
                ),
                step=1.0,
            )
        apply_price = st.form_submit_button("Apply pricing", type="primary")

    if apply_price:
        try:
            pricing = (
                price_from_spread(
                    breakdown["pricing_base_per_1000"],
                    breakdown["net_weight_kg_per_1000"],
                    value,
                )
                if basis == "Target spread"
                else spread_from_price(
                    breakdown["pricing_base_per_1000"],
                    breakdown["net_weight_kg_per_1000"],
                    value,
                )
            )
            st.session_state.pricing = pricing
            st.session_state.draft["target_spread_per_tonne"] = pricing[
                "target_spread_per_tonne"
            ]
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    if st.session_state.get("pricing"):
        pricing = st.session_state.pricing
        columns = st.columns(3)
        columns[0].metric(
            "Selling price / 1,000", f"£{pricing['selling_price_per_1000']:,.2f}"
        )
        columns[1].metric(
            "Selling price / item", f"£{pricing['selling_price_per_item']:,.4f}"
        )
        columns[2].metric(
            "Spread", f"£{pricing['target_spread_per_tonne']:,.2f} / tonne"
        )
        if pricing["target_spread_per_tonne"] < 0:
            st.warning("The selected selling price produces a negative spread.")
        if st.button("Continue to save and print", type="primary"):
            navigate_to(4)


def current_record() -> dict[str, Any]:
    return {
        **st.session_state.draft,
        **st.session_state.breakdown,
        **st.session_state.pricing,
        "quote_reference": st.session_state.get("quote_reference", ""),
        "customer_contact": st.session_state.get("customer_contact", ""),
        "notes": st.session_state.get("quote_notes", ""),
    }


def render_save(repository: CsvRepository, user_email: str, user_name: str) -> None:
    st.subheader("Save, quote and export")
    draft = st.session_state.draft
    st.session_state.setdefault(
        "quote_reference", f"Q-{datetime.now():%Y%m%d}-{str(draft['item_code'])[-6:]}"
    )
    st.session_state.setdefault("customer_contact", "")
    st.session_state.setdefault("quote_notes", "")

    left, right = st.columns(2)
    left.text_input("Quote reference", key="quote_reference")
    right.text_input("Customer contact", key="customer_contact")
    st.text_area("Quote notes", key="quote_notes", height=100)

    record = current_record()
    columns = st.columns(4)
    columns[0].metric("Item", str(record["item_code"]))
    columns[1].metric("Quantity", f"{float(record['order_quantity']):,.0f}")
    columns[2].metric(
        "Pricing base / 1,000", f"£{record['pricing_base_per_1000']:,.2f}"
    )
    columns[3].metric("Sell / 1,000", f"£{record['selling_price_per_1000']:,.2f}")

    if st.button("Save as a new revision", type="primary", width="stretch"):
        record["source_item_code"] = draft.get("source_item_code", "")
        saved = repository.save_costing(
            record, user_email=user_email, user_name=user_name
        )
        st.session_state.last_saved = saved
        st.success(
            f"Saved {saved['costing_id']} — {saved['item_code']} revision {saved['revision']}."
        )

    st.markdown("#### Downloads")
    download_columns = st.columns(3)
    download_columns[0].download_button(
        "Customer quote PDF",
        data=quote_pdf(record),
        file_name=f"{record['quote_reference'] or 'draft-quote'}.pdf",
        mime="application/pdf",
        width="stretch",
    )
    download_columns[1].download_button(
        "Indicative Sage item CSV",
        data=sage_stock_import_csv(record),
        file_name=f"{record['item_code']}-sage-import.csv",
        mime="text/csv",
        width="stretch",
    )
    one_row = pd.DataFrame([record]).to_csv(index=False).encode("utf-8-sig")
    download_columns[2].download_button(
        "Costing CSV",
        data=one_row,
        file_name=f"{record['item_code']}-costing.csv",
        mime="text/csv",
        width="stretch",
    )
    st.info(
        "The Sage export headings are a safe prototype. They must be mapped to the exact Sage 200 import template before production use."
    )


def render_history(repository: CsvRepository, current_user: str) -> None:
    st.header("Costing history")
    history = repository.load_history()
    if history.empty:
        st.info("No costings have been saved yet.")
        return

    filters = st.columns(2)
    user_options = ["All users", *sorted(history["created_by"].dropna().unique())]
    default_user = current_user if current_user in user_options else "All users"
    selected_user = filters[0].selectbox(
        "Created by", user_options, index=user_options.index(default_user)
    )
    item_options = ["All items", *sorted(history["item_code"].dropna().unique())]
    selected_item = filters[1].selectbox("Item", item_options)

    filtered = history.copy()
    if selected_user != "All users":
        filtered = filtered[filtered["created_by"] == selected_user]
    if selected_item != "All items":
        filtered = filtered[filtered["item_code"] == selected_item]
    filtered = filtered.sort_values("created_at_utc", ascending=False)

    visible_columns = [
        "created_at_utc",
        "created_by_name",
        "item_code",
        "revision",
        "customer_name",
        "description",
        "order_quantity",
        "pricing_base_per_1000",
        "selling_price_per_1000",
        "target_spread_per_tonne",
        "costing_id",
    ]
    st.dataframe(
        filtered[visible_columns],
        hide_index=True,
        width="stretch",
        column_config={
            "pricing_base_per_1000": st.column_config.NumberColumn(format="£%.2f"),
            "selling_price_per_1000": st.column_config.NumberColumn(format="£%.2f"),
            "target_spread_per_tonne": st.column_config.NumberColumn(format="£%.2f"),
            "order_quantity": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    columns = st.columns(2)
    columns[0].download_button(
        "Download filtered history CSV",
        data=filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name="costing-history.csv",
        mime="text/csv",
        width="stretch",
    )
    columns[1].download_button(
        "Print-friendly history PDF",
        data=history_pdf(filtered),
        file_name="costing-history.pdf",
        mime="application/pdf",
        width="stretch",
    )


def render_workflow(
    repository: CsvRepository,
    rate_table: HaulierRateTable,
    user_email: str,
    user_name: str,
) -> None:
    st.markdown(
        '<div class="brand-banner"><div><div class="brand-name">Solidus</div>'
        '<div class="brand-tagline">Your circular packaging partner</div></div>'
        '<div class="brand-tool">Spread Costing Tool</div></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Create auditable material-and-spread costings, quotes and stock-item export drafts."
    )
    stage_navigation()
    if st.session_state.step == 0:
        render_select(repository)
    elif st.session_state.step == 1:
        render_specification()
    elif st.session_state.step == 2:
        render_costs(repository, rate_table)
    elif st.session_state.step == 3:
        render_pricing()
    else:
        render_save(repository, user_email, user_name)


def main() -> None:
    user = require_user()
    repository = CsvRepository(data_directory(PROJECT_ROOT))
    rate_table = HaulierRateTable(repository.haulier_path)
    st.session_state.setdefault("step", 0)

    st.sidebar.markdown("## Solidus")
    st.sidebar.caption("Your circular packaging partner")
    st.sidebar.markdown("### Spread Costing Tool")
    st.sidebar.caption(f"Signed in as {user.name}")
    page = st.sidebar.radio("Navigation", ["Costing workflow", "History"])
    st.sidebar.divider()
    st.sidebar.caption(
        "Inputs: current items, BOMs, board stock, April 2026 mill prices and haulier rates\n\nOutput: append-only saved_costings.csv"
    )
    sign_out_button()

    if page == "History":
        render_history(repository, user.email)
    else:
        render_workflow(repository, rate_table, user.email, user.name)


if __name__ == "__main__":
    main()
