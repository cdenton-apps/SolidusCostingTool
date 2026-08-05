from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest


def _widget(group, label: str):
    return next(item for item in group if item.label == label)


def test_new_item_reaches_pricing_stage() -> None:
    app = AppTest.from_file("app.py", default_timeout=10).run()
    _widget(app.radio, "What would you like to cost?").set_value("New item").run()
    _widget(app.button, "Create a new costing").click().run()

    _widget(app.text_input, "Customer *").set_value("App Test Customer")
    _widget(app.text_input, "Item code *").set_value("APP-TEST-001")
    _widget(app.text_input, "Description *").set_value("Application test item")
    _widget(app.text_input, "Delivery postcode *").set_value("BD20 0AA")
    _widget(app.number_input, "Grade / GSM *").set_value(1000)
    _widget(app.number_input, "Length (mm) *").set_value(574)
    _widget(app.number_input, "Width (mm) *").set_value(376)
    _widget(app.number_input, "Height (mm) *").set_value(149)
    _widget(app.number_input, "Order quantity (units) *").set_value(10000)
    _widget(app.button, "Save order details").click().run()

    assert all("tooling" not in widget.label.lower() for widget in app.number_input)

    _widget(app.selectbox, "Board item *").set_value(
        "BRD001/101/LPB/1000G/BW"
    ).run()
    _widget(app.selectbox, "Other-component template *").set_value(
        "__NONE__"
    ).run()
    _widget(app.button, "Calculate pricing base").click().run()
    _widget(app.button, "Continue to pricing").click().run()

    assert not app.exception
    assert "Set spread or selling price" in [item.value for item in app.subheader]
    assert _widget(app.number_input, "Spread (%)")
    assert _widget(app.number_input, "Selling price per 1,000 (£)")


def test_spread_and_selling_price_inputs_stay_in_sync() -> None:
    app = AppTest.from_file("app.py", default_timeout=10)
    app.session_state["step"] = 3
    app.session_state["draft"] = {
        "customer_name": "Pricing test",
        "item_code": "PRICE-TEST",
        "description": "Pricing test item",
        "material": "Solid board",
        "board_gsm": 1000,
        "length_mm": 500,
        "width_mm": 400,
        "height_mm": 100,
        "pallet_quantity": 1000,
        "order_quantity": 10000,
        "delivery_postcode": "BD20 0AA",
        "spread_percent": 30,
    }
    app.session_state["breakdown"] = {
        "pricing_base_per_1000": 100.0,
        "pricing_base_per_item": 0.1,
        "pallet_count": 10.0,
        "net_weight_kg_per_1000": 500.0,
        "materials_cost_per_1000": 90.0,
        "manual_adjustment_per_1000": 0.0,
        "transport_cost_per_1000": 10.0,
    }
    app.run()

    pricing_base = app.session_state["breakdown"]["pricing_base_per_1000"]
    _widget(app.number_input, "Spread (%)").set_value(40).run()
    assert _widget(
        app.number_input, "Selling price per 1,000 (£)"
    ).value == pytest.approx(pricing_base / 0.6, abs=0.01)

    _widget(app.number_input, "Selling price per 1,000 (£)").set_value(
        pricing_base * 2
    ).run()
    assert _widget(app.number_input, "Spread (%)").value == pytest.approx(50)


def test_existing_item_specification_is_collapsed() -> None:
    app = AppTest.from_file("app.py", default_timeout=10).run()
    _widget(app.button, "Use this item").click().run()

    specification = next(
        item
        for item in app.expander
        if item.label == "View or amend product specification"
    )
    assert specification.proto.expanded is False
    assert "Order and fulfilment" in [item.value for item in app.subheader]


def test_mtc_can_be_entered_in_pallets() -> None:
    app = AppTest.from_file("app.py", default_timeout=10).run()
    _widget(app.radio, "What would you like to cost?").set_value("New item").run()
    _widget(app.button, "Create a new costing").click().run()

    _widget(app.text_input, "Customer *").set_value("MTC Test Customer")
    _widget(app.text_input, "Item code *").set_value("MTC-TEST-001")
    _widget(app.text_input, "Description *").set_value("Contract test item")
    _widget(app.text_input, "Delivery postcode *").set_value("BD20 0AA")
    _widget(app.number_input, "Grade / GSM *").set_value(1000)
    _widget(app.number_input, "Length (mm) *").set_value(574)
    _widget(app.number_input, "Width (mm) *").set_value(376)
    _widget(app.number_input, "Height (mm) *").set_value(149)
    _widget(app.radio, "Fulfilment type").set_value("MTC — Make to Contract").run()
    assert all(
        widget.label != "Stock holding target (%)" for widget in app.number_input
    )
    _widget(app.radio, "Enter order quantity as").set_value("Pallets").run()
    _widget(app.number_input, "Order quantity (pallets) *").set_value(10).run()
    _widget(app.number_input, "Pallets per delivery / call-off *").set_value(1)
    _widget(
        app.number_input, "Potential holding charge (£ per pallet per week)"
    ).set_value(3)
    _widget(app.button, "Save order details").click().run()

    assert not app.exception
    assert app.session_state["draft"]["order_quantity"] == 10_000
    assert app.session_state["draft"]["order_pallets"] == 10
    assert app.session_state["draft"]["estimated_delivery_count"] == 10
