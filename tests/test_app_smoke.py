from __future__ import annotations

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
    _widget(app.number_input, "Order quantity *").set_value(10000)
    _widget(app.button, "Save specification").click().run()

    _widget(app.button, "Calculate total cost").click().run()
    _widget(app.button, "Continue to pricing").click().run()

    assert not app.exception
    assert "Set margin or selling price" in [item.value for item in app.subheader]
