from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from src.auth import require_user, sign_out_button
from src.calculations import (
    calculate_cost,
    margin_from_price,
    price_from_margin,
    validate_details,
)
from src.exports import history_pdf, quote_pdf, sage_stock_import_csv
from src.repository import CsvRepository, SPECIFICATION_COLUMNS, data_directory


PROJECT_ROOT = Path(__file__).resolve().parent
STAGES = ["1 · Select", "2 · Specification", "3 · Costs", "4 · Price", "5 · Save"]

st.set_page_config(
    page_title="Costing Tool",
    page_icon="🧮",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root { --navy: #16324f; --teal: #1f7a6d; --pale: #eaf4f1; }
    .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1280px; }
    h1, h2, h3 { color: var(--navy); letter-spacing: -0.02em; }
    div[data-testid="stMetric"] { background: #f7faf9; border: 1px solid #dce7e3;
        border-radius: 12px; padding: 12px 16px; }
    div[data-testid="stForm"] { border-color: #dce7e3; border-radius: 14px; }
    .status-card { padding: 1rem 1.1rem; border-radius: 12px; background: var(--pale);
        border-left: 5px solid var(--teal); margin: .5rem 0 1rem; }
    .small-note { color: #52606d; font-size: .9rem; }
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
        "description": "",
        "material": "Solid board",
        "product_group": "Finished goods",
        "board_gsm": 1_000.0,
        "blank_length_mm": 500.0,
        "blank_width_mm": 400.0,
        "pallet_quantity": 1_000,
        "order_quantity": 10_000,
        "material_cost_per_tonne": 750.0,
        "bom_cost_per_1000": 0.0,
        "print_cost_per_1000": 0.0,
        "conversion_cost_per_1000": 0.0,
        "packing_cost_per_1000": 0.0,
        "fixed_tooling_cost": 0.0,
        "waste_percent": 5.0,
        "delivery_postcode": "",
        "delivery_method": "Haulier",
        "transport_rate_per_pallet": 45.0,
        "preferred_margin_percent": 30.0,
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
        "Total cost / 1,000", f"£{breakdown['total_cost_per_1000']:,.2f}"
    )
    metric_columns[1].metric("Cost per item", f"£{breakdown['cost_per_item']:,.4f}")
    metric_columns[2].metric("Pallets", f"{breakdown['pallet_count']:,.0f}")
    metric_columns[3].metric(
        "Gross kg / 1,000", f"{breakdown['gross_weight_kg_per_1000']:,.2f}"
    )
    rows = [
        ("Material (including waste)", breakdown["material_cost_per_1000"]),
        ("BOM / bought-in components", breakdown["bom_cost_per_1000"]),
        ("Print", breakdown["print_cost_per_1000"]),
        ("Conversion", breakdown["conversion_cost_per_1000"]),
        ("Packing", breakdown["packing_cost_per_1000"]),
        ("Tooling allocation", breakdown["tooling_cost_per_1000"]),
        ("Transport", breakdown["transport_cost_per_1000"]),
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
        columns = st.columns(4)
        columns[0].metric("Material", str(selected.get("material", "—")))
        columns[1].metric("GSM", f"{float(selected.get('board_gsm', 0)):,.0f}")
        columns[2].metric(
            "Pallet quantity", f"{float(selected.get('pallet_quantity', 0)):,.0f}"
        )
        columns[3].metric("Source", str(selected.get("source_type", "Feed")))
        st.caption(
            "You can use this item unchanged or alter any field. Saving always creates a new revision."
        )
        if st.button("Use this item", type="primary"):
            draft = default_draft()
            draft.update(
                {key: selected.get(key, draft.get(key)) for key in SPECIFICATION_COLUMNS}
            )
            if "preferred_margin_percent" in selected:
                draft["preferred_margin_percent"] = selected["preferred_margin_percent"]
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
        material_options = ["Solid board", "Corrugated", "Fibre", "Other"]
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
            "GSM *", min_value=1.0, value=draft_number("board_gsm", 1_000), step=25.0
        )

        col1, col2, col3 = st.columns(3)
        blank_length_mm = col1.number_input(
            "Blank length (mm) *",
            min_value=1.0,
            value=draft_number("blank_length_mm", 500),
            step=1.0,
        )
        blank_width_mm = col2.number_input(
            "Blank width (mm) *",
            min_value=1.0,
            value=draft_number("blank_width_mm", 400),
            step=1.0,
        )
        waste_percent = col3.number_input(
            "Material waste %",
            min_value=0.0,
            max_value=99.99,
            value=draft_number("waste_percent", 5),
            step=0.25,
        )

        col1, col2, col3 = st.columns(3)
        pallet_quantity = col1.number_input(
            "Pallet quantity *",
            min_value=1,
            value=max(1, int(draft_number("pallet_quantity", 1_000))),
            step=1,
        )
        order_quantity = col2.number_input(
            "Order quantity *",
            min_value=1,
            value=max(1, int(draft_number("order_quantity", 10_000))),
            step=1_000,
        )
        material_cost_per_tonne = col3.number_input(
            "Material cost per tonne (£) *",
            min_value=0.01,
            value=draft_number("material_cost_per_tonne", 750),
            step=10.0,
        )

        col1, col2 = st.columns(2)
        bom_cost_per_1000 = col1.number_input(
            "BOM / component cost per 1,000 (£)",
            min_value=0.0,
            value=draft_number("bom_cost_per_1000"),
            step=1.0,
            help="Existing items can be prefilled from data/bom_costs.csv.",
        )
        delivery_postcode = col2.text_input(
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
            "blank_length_mm": blank_length_mm,
            "blank_width_mm": blank_width_mm,
            "waste_percent": waste_percent,
            "pallet_quantity": pallet_quantity,
            "order_quantity": order_quantity,
            "material_cost_per_tonne": material_cost_per_tonne,
            "bom_cost_per_1000": bom_cost_per_1000,
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


def render_costs() -> None:
    st.subheader("Production and transport costs")
    draft = st.session_state.draft
    st.caption(
        "Transport currently uses pallets × rate per pallet. The fuller transport-app rules can replace this module without changing the screens."
    )
    with st.form("cost_form"):
        col1, col2, col3 = st.columns(3)
        print_cost = col1.number_input(
            "Print cost per 1,000 (£)",
            min_value=0.0,
            value=draft_number("print_cost_per_1000"),
            step=1.0,
        )
        conversion_cost = col2.number_input(
            "Conversion cost per 1,000 (£)",
            min_value=0.0,
            value=draft_number("conversion_cost_per_1000"),
            step=1.0,
        )
        packing_cost = col3.number_input(
            "Packing cost per 1,000 (£)",
            min_value=0.0,
            value=draft_number("packing_cost_per_1000"),
            step=1.0,
        )
        col1, col2, col3 = st.columns(3)
        fixed_tooling = col1.number_input(
            "Fixed tooling / setup (£)",
            min_value=0.0,
            value=draft_number("fixed_tooling_cost"),
            step=10.0,
        )
        methods = ["Haulier", "Customer collection", "Included elsewhere"]
        current_method = str(draft.get("delivery_method", "Haulier"))
        delivery_method = col2.selectbox(
            "Delivery method", methods, index=methods.index(current_method)
        )
        transport_rate = col3.number_input(
            "Transport rate per pallet (£)",
            min_value=0.0,
            value=draft_number("transport_rate_per_pallet", 45),
            step=1.0,
            disabled=delivery_method != "Haulier",
        )
        calculate = st.form_submit_button("Calculate total cost", type="primary")

    if calculate:
        if delivery_method == "Haulier" and transport_rate <= 0:
            st.error("Enter a transport rate, or choose a non-haulier delivery method.")
        else:
            st.session_state.draft.update(
                {
                    "print_cost_per_1000": print_cost,
                    "conversion_cost_per_1000": conversion_cost,
                    "packing_cost_per_1000": packing_cost,
                    "fixed_tooling_cost": fixed_tooling,
                    "delivery_method": delivery_method,
                    "transport_rate_per_pallet": transport_rate,
                }
            )
            st.session_state.breakdown = calculate_cost(st.session_state.draft)
            st.session_state.pop("pricing", None)
            st.rerun()

    if st.session_state.get("breakdown"):
        show_cost_breakdown(st.session_state.breakdown)
        if st.button("Continue to pricing", type="primary"):
            navigate_to(3)


def render_pricing() -> None:
    st.subheader("Set margin or selling price")
    breakdown = st.session_state.breakdown
    show_cost_breakdown(breakdown)
    basis = st.radio(
        "Which value do you want to control?",
        ["Preferred margin", "Selling price"],
        horizontal=True,
    )
    with st.form("pricing_form"):
        if basis == "Preferred margin":
            value = st.number_input(
                "Preferred margin %",
                min_value=0.0,
                max_value=99.99,
                value=float(
                    st.session_state.get("pricing", {}).get(
                        "preferred_margin_percent",
                        draft_number("preferred_margin_percent", 30),
                    )
                ),
                step=0.25,
            )
        else:
            suggested = price_from_margin(
                breakdown["total_cost_per_1000"],
                draft_number("preferred_margin_percent", 30),
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
                price_from_margin(breakdown["total_cost_per_1000"], value)
                if basis == "Preferred margin"
                else margin_from_price(breakdown["total_cost_per_1000"], value)
            )
            st.session_state.pricing = pricing
            st.session_state.draft["preferred_margin_percent"] = pricing[
                "preferred_margin_percent"
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
        columns[2].metric("Margin", f"{pricing['preferred_margin_percent']:,.2f}%")
        if pricing["preferred_margin_percent"] < 0:
            st.warning("The selected selling price produces a negative margin.")
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
    columns[2].metric("Cost / 1,000", f"£{record['total_cost_per_1000']:,.2f}")
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
        "total_cost_per_1000",
        "selling_price_per_1000",
        "preferred_margin_percent",
        "costing_id",
    ]
    st.dataframe(
        filtered[visible_columns],
        hide_index=True,
        width="stretch",
        column_config={
            "total_cost_per_1000": st.column_config.NumberColumn(format="£%.2f"),
            "selling_price_per_1000": st.column_config.NumberColumn(format="£%.2f"),
            "preferred_margin_percent": st.column_config.NumberColumn(format="%.2f%%"),
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


def render_workflow(repository: CsvRepository, user_email: str, user_name: str) -> None:
    st.title("Costing Tool")
    st.caption("Create auditable product costings, quotes and stock-item export drafts.")
    stage_navigation()
    if st.session_state.step == 0:
        render_select(repository)
    elif st.session_state.step == 1:
        render_specification()
    elif st.session_state.step == 2:
        render_costs()
    elif st.session_state.step == 3:
        render_pricing()
    else:
        render_save(repository, user_email, user_name)


def main() -> None:
    user = require_user()
    repository = CsvRepository(data_directory(PROJECT_ROOT))
    st.session_state.setdefault("step", 0)

    st.sidebar.markdown("### Costing Tool")
    st.sidebar.caption(f"Signed in as {user.name}")
    page = st.sidebar.radio("Navigation", ["Costing workflow", "History"])
    st.sidebar.divider()
    st.sidebar.caption(
        "Inputs: current_items.csv and bom_costs.csv\n\nOutput: append-only saved_costings.csv"
    )
    sign_out_button()

    if page == "History":
        render_history(repository, user.email)
    else:
        render_workflow(repository, user.email, user.name)


if __name__ == "__main__":
    main()
