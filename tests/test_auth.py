from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.auth import (
    _verify_configured_password,
    authenticate_admin,
    configured_users_for_import,
    make_password_hash,
    verify_password,
)
import src.auth as auth_module


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


def test_inline_admin_check_requires_valid_admin_credentials(monkeypatch) -> None:
    sections = {
        "app_auth": {"mode": "password"},
        "users": {
            "manager": {
                "name": "Commercial Manager",
                "email": "manager@example.com",
                "password": "approval-password",
                "is_admin": True,
            },
            "standard": {
                "name": "Standard User",
                "email": "standard@example.com",
                "password": "user-password",
                "is_admin": False,
            },
        },
    }
    monkeypatch.setattr(
        auth_module, "_secret_section", lambda name: sections.get(name, {})
    )

    approved = authenticate_admin("manager", "approval-password")
    assert approved is not None
    assert approved.is_admin
    assert approved.username == "manager"
    assert authenticate_admin("manager", "wrong") is None
    assert authenticate_admin("standard", "user-password") is None


def test_secrets_users_are_prepared_for_safe_database_import(monkeypatch) -> None:
    sections = {
        "users": {
            "creator": {
                "name": "Product Creator",
                "email": "creator@example.com",
                "password": "temporary-password",
                "can_create_new": True,
            }
        }
    }
    monkeypatch.setattr(
        auth_module, "_secret_section", lambda name: sections.get(name, {})
    )

    imported = configured_users_for_import()

    assert len(imported) == 1
    assert imported[0]["role"] == "creator"
    assert imported[0]["must_change_password"] is True
    assert "temporary-password" not in imported[0]["password_hash"]
    assert verify_password("temporary-password", imported[0]["password_hash"])


def test_malformed_configured_hash_is_not_imported(monkeypatch) -> None:
    sections = {
        "users": {
            "broken": {
                "name": "Broken Hash",
                "email": "broken@example.com",
                "password_hash": "password1",
            }
        }
    }
    monkeypatch.setattr(
        auth_module, "_secret_section", lambda name: sections.get(name, {})
    )

    assert configured_users_for_import() == []


def test_database_admin_credentials_take_precedence() -> None:
    password_hash = make_password_hash("database-admin-password")

    class Repository:
        login_recorded = False
        failures = 0

        def has_app_users(self):
            return True

        def app_user_login_security(self, username):
            return {"is_locked": False, "failed_attempts": 0}

        def record_login_failure(self, username):
            self.failures += 1

        def record_user_login(self, username):
            self.login_recorded = True

        def get_app_user(self, username):
            if username.casefold() != "manager":
                return None
            return {
                "username": "manager",
                "name": "Commercial Manager",
                "email": "manager@example.com",
                "password_hash": password_hash,
                "role": "admin",
                "can_view_history": True,
                "is_active": True,
                "must_change_password": False,
                "session_version": 1,
            }

    approved = authenticate_admin(
        "manager", "database-admin-password", Repository()
    )

    assert approved is not None
    assert approved.is_admin
    assert authenticate_admin("manager", "wrong", Repository()) is None


def test_database_admin_credentials_respect_temporary_lock() -> None:
    class Repository:
        failures = 0

        def has_app_users(self):
            return True

        def app_user_login_security(self, username):
            return {"is_locked": True, "failed_attempts": 5}

        def get_app_user(self, username):
            raise AssertionError("a locked password must not be checked")

        def record_login_failure(self, username):
            self.failures += 1

    repository = Repository()
    assert authenticate_admin("manager", "anything", repository) is None
    assert repository.failures == 0


def test_app_is_locked_when_no_users_are_configured() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=10).run()

    assert any(
        "No users are set up" in item.value for item in app.error
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
            "can_view_history": True,
            "is_admin": True,
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

    expected_user = {
        "username": "connor",
        "email": "connor@example.com",
        "name": "Connor Denton",
        "can_create_new": True,
        "can_view_history": True,
        "is_admin": True,
        "role": "admin",
        "must_change_password": False,
        "session_version": 1,
    }
    authenticated_user = app.session_state["authenticated_user"]
    assert {
        key: authenticated_user[key] for key in expected_user
    } == expected_user
    assert _widget(app.radio, "Costing route")
    menu_labels = {button.label for button in app.button}
    assert "Team history" in menu_labels
    assert "Dashboard" in menu_labels
    assert "User activity" not in menu_labels
    assert "Admin tools" in menu_labels
    assert any(
        "Signed in as Connor Denton" in item.value
        for item in app.caption
    )

    _widget(app.button, "Dashboard").click().run()
    assert "Dashboard" in [item.value for item in app.header]
    assert any("Online now" in item.value for item in app.markdown)
    assert "Users and access" not in [item.value for item in app.subheader]

    _widget(app.button, "Admin tools").click().run()
    assert "Admin tools" in [item.value for item in app.header]

    _widget(app.button, "Sign out").click().run()
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
        "can_view_history": False,
    }
    app.run()

    assert _widget(app.selectbox, "Search existing products")
    assert all(item.label != "Costing route" for item in app.radio)
    assert _widget(app.button, "Costing workflow")
    assert all(item.label != "Team history" for item in app.button)
    assert all(item.label != "Create new product" for item in app.button)


def test_standard_user_cannot_resume_a_new_product_draft() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=10)
    app.secrets["app_auth"] = {"mode": "password"}
    app.session_state["authenticated_user"] = {
        "username": "standard",
        "email": "standard@example.com",
        "name": "Standard User",
        "can_create_new": False,
        "can_view_history": False,
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
