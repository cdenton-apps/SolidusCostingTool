from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.auth import (
    _verify_configured_password,
    make_password_hash,
    verify_password,
)


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _widget(group, label: str):
    return next(item for item in group if item.label == label)


def test_password_hash_round_trip() -> None:
    encoded = make_password_hash("a-secure-test-password")
    assert verify_password("a-secure-test-password", encoded)
    assert not verify_password("wrong-password", encoded)


def test_configured_password_accepts_plain_secret_or_hash() -> None:
    assert _verify_configured_password(
        "chosen-password", {"password": "chosen-password"}
    )
    assert not _verify_configured_password(
        "wrong-password", {"password": "chosen-password"}
    )

    encoded = make_password_hash("hashed-password")
    assert _verify_configured_password(
        "hashed-password", {"password_hash": encoded}
    )
    assert not _verify_configured_password(
        "wrong-password", {"password_hash": encoded}
    )


def test_app_is_locked_when_no_users_are_configured() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=10).run()

    assert any(
        "Login has not been configured" in item.value for item in app.error
    )
    assert not app.radio


def test_password_login_rejects_wrong_password_and_accepts_valid_user() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.secrets["users"] = {
        "connor": {
            "name": "Connor Denton",
            "email": "connor@example.com",
            "password": "correct-horse-battery-staple",
            "can_create_new": True,
        }
    }
    app.run()

    _widget(app.text_input, "Username").set_value("connor")
    _widget(app.text_input, "Password").set_value("wrong-password")
    _widget(app.button, "Sign in").click().run()
    assert any("not recognised" in item.value for item in app.error)

    _widget(app.text_input, "Username").set_value("connor")
    _widget(app.text_input, "Password").set_value(
        "correct-horse-battery-staple"
    )
    _widget(app.button, "Sign in").click().run()

    assert app.session_state["authenticated_user"] == {
        "username": "connor",
        "email": "connor@example.com",
        "name": "Connor Denton",
        "can_create_new": True,
    }
    assert _widget(app.radio, "Costing route")
    assert any(
        "Signed in as Connor Denton" in item.value
        for item in app.sidebar.caption
    )

    _widget(app.sidebar.button, "Sign out").click().run()
    assert "authenticated_user" not in app.session_state
    assert _widget(app.text_input, "Username")


def test_standard_user_sees_existing_products_only() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.session_state["authenticated_user"] = {
        "username": "standard",
        "email": "standard@example.com",
        "name": "Standard User",
        "can_create_new": False,
    }
    app.run()

    assert _widget(app.selectbox, "Search existing products")
    assert all(item.label != "Costing route" for item in app.radio)
    assert _widget(app.sidebar.radio, "Navigation")
    assert all(item.label != "Create new product" for item in app.button)


def test_standard_user_cannot_resume_a_new_product_draft() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.session_state["authenticated_user"] = {
        "username": "standard",
        "email": "standard@example.com",
        "name": "Standard User",
        "can_create_new": False,
    }
    app.session_state["step"] = 1
    app.session_state["draft"] = {
        "item_code": "UNAUTHORISED-NEW",
        "source_item_code": "",
    }
    app.run()

    assert app.session_state["step"] == 0
    assert "draft" not in app.session_state
    assert any("existing products only" in item.value for item in app.warning)
