from __future__ import annotations

from streamlit.testing.v1 import AppTest


def _widget(group, label: str):
    return next(item for item in group if item.label == label)


def test_new_item_reaches_export_stage() -> None:
    app = AppTest.from_file("app.py", default_timeout=10).run()
    _widget(app.radio, "What would you like to cost?").set_value("New item").run()
    _widget(app.button, "Create a new costing").click().run()

    _widget(app.text_input, "Customer *").set_value("App Test Customer")
    _widget(app.text_input, "Item code *").set_value("APP-TEST-001")
    _widget(app.text_input, "Description *").set_value("Application test item")
    _widget(app.text_input, "Delivery postcode *").set_value("BD20 0AA")
    _widget(app.button, "Save specification").click().run()

    _widget(app.button, "Calculate total cost").click().run()
    _widget(app.button, "Continue to pricing").click().run()
    _widget(app.button, "Apply pricing").click().run()
    _widget(app.button, "Continue to save and print").click().run()

    assert not app.exception
    assert {item.label for item in app.get("download_button")} == {
        "Customer quote PDF",
        "Indicative Sage item CSV",
        "Costing CSV",
    }

