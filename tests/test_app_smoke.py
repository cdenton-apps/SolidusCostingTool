from __future__ import annotations

from pathlib import Path
from shutil import copy2

import pytest
from streamlit.testing.v1 import AppTest

from src.repository import CsvRepository


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
PROJECT_DATA = APP_PATH.parent / "data"


def _widget(group, label: str):
    return next(item for item in group if item.label == label)


def _demo_app() -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "demo"}
    return app


def test_new_item_reaches_pricing_stage() -> None:
    app = _demo_app().run()
    _widget(app.radio, "Costing route").set_value("New product").run()
    _widget(app.button, "Create new product").click().run()

    _widget(app.text_input, "Customer *").set_value("App Test Customer")
    _widget(app.text_input, "Item code *").set_value("APP-TEST-001")
    _widget(app.text_input, "Description *").set_value("Application test item")
    _widget(app.text_input, "Delivery postcode *").set_value("BD20 0AA")
    _widget(app.number_input, "Grade / GSM *").set_value(1000)
    _widget(app.number_input, "Length (mm) *").set_value(574)
    _widget(app.number_input, "Width (mm) *").set_value(376)
    _widget(app.number_input, "Height (mm) *").set_value(149)
    _widget(app.number_input, "Order quantity (units) *").set_value(10000)
    _widget(app.number_input, "Expected annual volume (units) *").set_value(10_000)
    _widget(app.checkbox, "Consistent Payer").check()
    _widget(app.button, "Save order details").click().run()

    assert app.session_state["draft"]["annual_volume_band"] == "0 - 10,000"
    assert app.session_state["draft"]["annual_volume_units"] == 10_000
    assert app.session_state["draft"]["comex_consistent_payer"] is True

    assert all("tooling" not in widget.label.lower() for widget in app.number_input)
    assert all(
        "commercial adjustment" not in widget.label.lower()
        for widget in app.number_input
    )

    _widget(app.selectbox, "Board item *").set_value(
        "BRD001/101/LPB/1000G/BW"
    ).run()
    _widget(app.selectbox, "Other-component template *").set_value(
        "__NONE__"
    ).run()
    _widget(app.button, "Calculate pricing base").click().run()
    assert app.session_state["breakdown"][
        "total_material_adjustment_percent"
    ] == pytest.approx(10)
    _widget(app.button, "Continue to pricing").click().run()

    assert not app.exception
    assert "Set spread or selling price" in [item.value for item in app.subheader]
    assert _widget(app.number_input, "Spread (%)")
    assert _widget(app.number_input, "Selling price per 1,000 (£)")


def test_new_item_can_fill_board_details_from_known_code() -> None:
    app = _demo_app().run()
    _widget(app.radio, "Costing route").set_value("New product").run()
    _widget(app.button, "Create new product").click().run()

    _widget(app.text_input, "Board code").set_value("4-15614/").run()
    _widget(app.button, "Fill board details from code").click().run()

    assert _widget(app.text_input, "Board code").value == "4-15614"
    assert _widget(app.number_input, "Grade / GSM *").value == pytest.approx(1000)
    assert _widget(
        app.number_input, "Board width / reel width (mm)"
    ).value == pytest.approx(1358)
    assert _widget(
        app.number_input, "Board length / chop (mm)"
    ).value == pytest.approx(878)
    assert app.session_state["draft"]["board_price_per_tonne"] == pytest.approx(794)


def test_spread_and_selling_price_inputs_stay_in_sync() -> None:
    app = _demo_app()
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
        "bom_available": 1,
        "source_item_code": "BOX001/101/LPB/1000G/1240P",
    }
    app.session_state["breakdown"] = {
        "pricing_base_per_1000": 100.0,
        "pricing_base_per_item": 0.1,
        "pallet_count": 10.0,
        "net_weight_kg_per_1000": 500.0,
        "materials_cost_per_1000": 90.0,
        "manual_adjustment_per_1000": 0.0,
        "transport_cost_per_1000": 10.0,
        "machine_hours_per_1000": 0.5,
        "total_machine_hours": 5.0,
    }
    # Reproduce the stale widget state seen after the deployed app update.
    app.session_state["pricing_base_for_inputs"] = 100.0
    app.session_state["pricing"] = {
        "spread_percent": 30.0,
        "spread_value_per_1000": 42.8571,
        "selling_price_per_1000": 142.8571,
        "selling_price_per_item": 0.14286,
    }
    app.session_state["spread_percent_input"] = -100_000.0
    app.session_state["selling_price_input"] = 0.01
    app.run()

    assert _widget(app.number_input, "Spread (%)").value == pytest.approx(30)
    assert _widget(
        app.number_input, "Selling price per 1,000 (£)"
    ).value == pytest.approx(142.8571)
    rendered_cards = "\n".join(item.value for item in app.markdown)
    assert "Spread / machine hour" in rendered_cards
    assert "Material spread / 1,000" in rendered_cards
    assert "5.00 h · 5 hr 0 min" in rendered_cards
    assert any(
        item.label == "View machine hours calculation" for item in app.expander
    )
    assert any(
        item.label == "How the material adjustment was calculated"
        for item in app.expander
    )

    pricing_base = app.session_state["breakdown"]["pricing_base_per_1000"]
    _widget(app.number_input, "Spread (%)").set_value(40).run()
    assert _widget(
        app.number_input, "Selling price per 1,000 (£)"
    ).value == pytest.approx(pricing_base / 0.6, abs=0.01)

    _widget(app.number_input, "Selling price per 1,000 (£)").set_value(
        pricing_base * 2
    ).run()
    assert _widget(app.number_input, "Spread (%)").value == pytest.approx(50)


def test_red_costing_requires_recorded_admin_override() -> None:
    app = _demo_app()
    app.session_state["step"] = 3
    app.session_state["draft"] = {
        "customer_name": "Approval test",
        "item_code": "APPROVAL-TEST",
        "description": "Approval test item",
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
        "material_base_per_1000": 90.0,
        "transport_cost_per_1000": 10.0,
        "machine_hours_per_1000": 0.5,
        "total_machine_hours": 5.0,
    }
    app.run()

    assert app.session_state["pricing"]["traffic_light_status"] == "red"
    assert _widget(app.button, "Continue to save and print").disabled
    _widget(app.text_area, "Reason for admin override *").set_value(
        "Approved for this customer agreement"
    ).run()
    _widget(app.button, "Approve red costing").click().run()

    assert app.session_state["pricing"]["traffic_override_approved"] is True
    assert app.session_state["pricing"]["traffic_override_by_username"] == "demo"
    assert not _widget(app.button, "Continue to save and print").disabled

    _widget(app.number_input, "Spread (%)").set_value(31).run()
    assert not app.session_state["pricing"].get("traffic_override_approved", False)
    assert _widget(app.button, "Continue to save and print").disabled


def test_red_costing_is_blocked_for_non_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for source in PROJECT_DATA.glob("*.csv"):
        copy2(source, tmp_path / source.name)
    monkeypatch.setenv("COSTING_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.secrets["users"] = {
        "manager": {
            "name": "Commercial Manager",
            "email": "manager@example.com",
            "password": "dummy-admin-passphrase",
            "is_admin": True,
        }
    }
    app.session_state["authenticated_user"] = {
        "username": "standard",
        "email": "standard@example.com",
        "name": "Standard User",
        "can_create_new": False,
        "can_view_history": False,
        "is_admin": False,
    }
    app.session_state["step"] = 3
    app.session_state["draft"] = {
        "customer_name": "Approval test",
        "item_code": "APPROVAL-TEST",
        "description": "Approval test item",
        "material": "Solid board",
        "board_gsm": 1000,
        "length_mm": 500,
        "width_mm": 400,
        "height_mm": 100,
        "pallet_quantity": 1000,
        "order_quantity": 10000,
        "delivery_postcode": "BD20 0AA",
        "spread_percent": 30,
        "source_item_code": "APPROVAL-TEST",
    }
    app.session_state["breakdown"] = {
        "pricing_base_per_1000": 100.0,
        "pricing_base_per_item": 0.1,
        "pallet_count": 10.0,
        "net_weight_kg_per_1000": 500.0,
        "materials_cost_per_1000": 90.0,
        "material_base_per_1000": 90.0,
        "transport_cost_per_1000": 10.0,
        "machine_hours_per_1000": 0.5,
        "total_machine_hours": 5.0,
    }
    app.run()

    assert app.session_state["pricing"]["traffic_light_status"] == "red"
    assert _widget(app.button, "Continue to save and print").disabled
    rendered_cards = "\n".join(item.value for item in app.markdown)
    assert "Pricing base / 1,000" not in rendered_cards
    assert "Spread value / 1,000" not in rendered_cards
    assert "Material spread / 1,000" not in rendered_cards
    assert "Spread / machine hour" in rendered_cards
    assert "Machine time for quote" not in rendered_cards
    assert all(
        item.label != "How the material adjustment was calculated"
        for item in app.expander
    )
    assert _widget(app.text_input, "Admin username")
    assert _widget(app.text_input, "Admin password")
    assert _widget(app.text_area, "Reason for admin override *")

    _widget(app.text_input, "Admin username").set_value("manager")
    _widget(app.text_input, "Admin password").set_value("dummy-admin-passphrase")
    _widget(app.text_area, "Reason for admin override *").set_value(
        "Commercial exception agreed"
    )
    _widget(app.button, "Approve red costing").click().run()

    assert app.session_state["pricing"]["traffic_override_approved"] is True
    assert app.session_state["pricing"]["traffic_override_by_username"] == "manager"
    assert not _widget(app.button, "Continue to save and print").disabled


def test_amber_warning_must_be_acknowledged() -> None:
    app = _demo_app()
    app.session_state["step"] = 3
    app.session_state["draft"] = {
        "customer_name": "Amber test",
        "item_code": "AMBER-TEST",
        "description": "Amber test item",
        "material": "Solid board",
        "board_gsm": 1000,
        "length_mm": 500,
        "width_mm": 400,
        "height_mm": 100,
        "pallet_quantity": 1000,
        "order_quantity": 10000,
        "delivery_postcode": "BD20 0AA",
        "spread_percent": 27,
    }
    app.session_state["breakdown"] = {
        "pricing_base_per_1000": 100.0,
        "pricing_base_per_item": 0.1,
        "pallet_count": 10.0,
        "net_weight_kg_per_1000": 500.0,
        "materials_cost_per_1000": 90.0,
        "material_base_per_1000": 90.0,
        "transport_cost_per_1000": 10.0,
        "machine_hours_per_1000": 0.05,
        "total_machine_hours": 0.5,
    }
    app.run()

    assert app.session_state["pricing"]["traffic_light_status"] == "amber"
    assert any("AMBER COMMERCIAL WARNING" in item.value for item in app.markdown)
    assert _widget(app.button, "Continue to save and print").disabled
    _widget(app.button, "Acknowledge amber warning").click().run()

    assert app.session_state["pricing"]["traffic_amber_acknowledged"] is True
    assert app.session_state["pricing"][
        "traffic_amber_acknowledged_by_username"
    ] == "demo"
    assert not _widget(app.button, "Continue to save and print").disabled

    _widget(app.number_input, "Spread (%)").set_value(28).run()
    assert not app.session_state["pricing"].get(
        "traffic_amber_acknowledged", False
    )
    assert _widget(app.button, "Continue to save and print").disabled


def test_exports_require_an_exact_saved_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for source in PROJECT_DATA.glob("*.csv"):
        copy2(source, tmp_path / source.name)
    monkeypatch.setenv("COSTING_DATA_DIR", str(tmp_path))
    app = _demo_app()
    app.session_state["step"] = 4
    app.session_state["draft"] = {
        "customer_name": "Saved quote customer",
        "item_code": "SAVE-BEFORE-PRINT",
        "description": "Saved quote test item",
        "material": "Solid board",
        "board_gsm": 1000,
        "length_mm": 500,
        "width_mm": 400,
        "height_mm": 100,
        "pallet_quantity": 1000,
        "order_quantity": 10000,
        "order_pallets": 10,
        "fulfilment_type": "MTO",
        "delivery_method": "Ex works",
        "delivery_postcode": "BD20 0AA",
        "source_item_code": "SAVE-BEFORE-PRINT",
    }
    app.session_state["breakdown"] = {
        "pricing_base_per_1000": 100.0,
        "pricing_base_per_item": 0.1,
        "materials_cost_per_1000": 100.0,
        "pallet_count": 10.0,
        "transport_total": 0.0,
        "transport_cost_per_1000": 0.0,
        "net_weight_kg_per_1000": 500.0,
    }
    app.session_state["pricing"] = {
        "spread_percent": 30.0,
        "spread_value_per_1000": 42.8571429,
        "selling_price_per_1000": 142.8571429,
        "selling_price_per_item": 0.1428571,
        "material_spread_value_per_1000": 42.8571429,
        "total_spread_value": 428.571429,
        "total_machine_hours": 0.0,
        "spread_per_machine_hour": 0.0,
    }
    app.run()

    assert not app.download_button
    assert any(
        "Save this revision before downloading" in item.value
        for item in app.warning
    )

    _widget(app.button, "Save as a new revision").click().run()
    assert {button.label for button in app.download_button} == {
        "Customer quote PDF",
        "Costing CSV",
        "Sage stock import CSV",
    }
    assert len(CsvRepository(tmp_path).load_history()) == 1

    _widget(app.text_area, "Quote notes").set_value("Changed after saving").run()
    assert not app.download_button
    _widget(app.button, "Save as a new revision").click().run()
    assert len(CsvRepository(tmp_path).load_history()) == 2
    assert app.download_button


def test_existing_item_specification_is_collapsed() -> None:
    app = _demo_app().run()
    _widget(app.selectbox, "Search existing products").set_value(0).run()
    _widget(app.button, "Start costing").click().run()

    specification = next(
        item
        for item in app.expander
        if item.label == "View or amend product specification"
    )
    assert specification.proto.expanded is False
    assert "Order and fulfilment" in [item.value for item in app.subheader]


def test_header_clearance_and_product_details_allow_wrapping() -> None:
    source = APP_PATH.read_text(encoding="utf-8")

    assert "padding-top: 4.75rem !important" in source
    assert "padding: 4.25rem 1rem 2rem !important" in source
    assert 'class="detail-grid"' in source
    assert "overflow-wrap: anywhere" in source


def test_user_facing_copy_avoids_development_language() -> None:
    copy = (APP_PATH.read_text(encoding="utf-8") + (APP_PATH.parent / "README.md").read_text(encoding="utf-8")).lower()

    assert "the aim is simple" not in copy
    assert "file you supplied" not in copy
    assert "costing data supplied" not in copy


def test_mtc_can_be_entered_in_pallets() -> None:
    app = _demo_app().run()
    _widget(app.radio, "Costing route").set_value("New product").run()
    _widget(app.button, "Create new product").click().run()

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
    _widget(app.number_input, "Expected annual volume (units) *").set_value(75_000)
    _widget(app.number_input, "Minimum pallets per delivery *").set_value(1)
    _widget(
        app.number_input, "Potential holding charge (£ per pallet per week)"
    ).set_value(3)
    _widget(app.button, "Save order details").click().run()

    assert not app.exception
    assert app.session_state["draft"]["order_quantity"] == 10_000
    assert app.session_state["draft"]["order_pallets"] == 10
    assert app.session_state["draft"]["estimated_delivery_count"] == 10


def test_user_can_reopen_only_their_own_saved_costing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for source in PROJECT_DATA.glob("*.csv"):
        copy2(source, tmp_path / source.name)
    repository = CsvRepository(tmp_path)
    mine = repository.save_costing(
        {
            "item_code": "HISTORY-MINE",
            "source_item_code": "HISTORY-MINE",
            "customer_name": "History Customer",
            "customer_contact": "Alex Example",
            "description": "A saved product to amend",
            "material": "Solid board",
            "board_gsm": 1000,
            "length_mm": 500,
            "width_mm": 400,
            "height_mm": 100,
            "pallet_quantity": 1000,
            "order_quantity": 10000,
            "annual_volume_units": 75000,
            "delivery_postcode": "BD20 0AA",
            "spread_percent": 32,
            "notes": "Keep this note",
            "esign_signers": [
                {
                    "name": "Director",
                    "email": "director@example.com",
                    "status": "signed",
                }
            ],
        },
        user_username="standard",
        user_email="standard@example.com",
        user_name="Standard User",
    )
    theirs = repository.save_costing(
        {"item_code": "HISTORY-THEIRS", "customer_name": "Private Customer"},
        user_username="otheruser",
        user_email="other@example.com",
        user_name="Other User",
    )
    monkeypatch.setenv("COSTING_DATA_DIR", str(tmp_path))

    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.session_state["authenticated_user"] = {
        "username": "standard",
        "email": "standard@example.com",
        "name": "Standard User",
        "can_create_new": False,
        "can_view_history": False,
    }
    app.run()
    _widget(app.sidebar.radio, "Navigation").set_value("My costings").run()

    history_selector = _widget(app.selectbox, "Choose a costing to reopen")
    assert any("HISTORY-MINE" in option for option in history_selector.options)
    assert all("HISTORY-THEIRS" not in option for option in history_selector.options)
    history_selector.set_value(mine["costing_id"]).run()
    _widget(app.button, "Load and amend this costing").click().run()

    assert app.session_state["main_navigation"] == "Costing workflow"
    assert app.session_state["step"] == 1
    assert app.session_state["draft"]["customer_name"] == "History Customer"
    assert app.session_state["draft"]["source_item_code"] == "HISTORY-MINE"
    assert app.session_state["quote_notes"] == "Keep this note"
    assert app.session_state["customer_contact"] == "Alex Example"
    assert "quote_reference" not in app.session_state
    assert "Order and fulfilment" in [item.value for item in app.subheader]
    assert {button.label for button in app.button if button.label in {
        "Quote details", "Delivery", "Price & approval", "Save & send"
    }} == {"Quote details", "Delivery", "Price & approval", "Save & send"}
    assert all(
        button.label not in {"Product", "Order", "Costs", "Pricing", "Save / print"}
        for button in app.button
    )
    specification = next(
        item for item in app.expander if item.label == "View product specification"
    )
    assert specification.proto.expanded is False
    assert _widget(app.text_input, "Item code *").disabled
    _widget(app.button, "Delivery").click().run()
    assert app.session_state["step"] == 2
    assert "Delivery details" in [item.value for item in app.subheader]


def test_team_history_permission_shows_saved_usernames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for source in PROJECT_DATA.glob("*.csv"):
        copy2(source, tmp_path / source.name)
    repository = CsvRepository(tmp_path)
    repository.save_costing(
        {"item_code": "TEAM-ONE", "customer_name": "Customer One"},
        user_username="alice",
        user_email="alice@example.com",
        user_name="Alice Example",
    )
    repository.save_costing(
        {"item_code": "TEAM-TWO", "customer_name": "Customer Two"},
        user_username="bob",
        user_email="bob@example.com",
        user_name="Bob Example",
    )
    monkeypatch.setenv("COSTING_DATA_DIR", str(tmp_path))

    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.session_state["authenticated_user"] = {
        "username": "manager",
        "email": "manager@example.com",
        "name": "History Manager",
        "can_create_new": False,
        "can_view_history": True,
    }
    app.run()
    navigation = _widget(app.sidebar.radio, "Navigation")
    assert "Team history" in navigation.options
    navigation.set_value("Team history").run()

    table = app.dataframe[0].value
    assert set(table["created_by_username"]) >= {"alice", "bob"}
    assert all(item.label != "Load and amend this costing" for item in app.button)
