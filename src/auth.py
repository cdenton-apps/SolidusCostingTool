from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

import streamlit as st


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    email: str
    name: str
    can_create_new: bool = False
    can_view_history: bool = False


def make_password_hash(password: str, iterations: int = 600_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(iterations),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt.encode()),
            int(iterations),
        )
        return hmac.compare_digest(
            base64.urlsafe_b64encode(digest).decode(), expected
        )
    except (TypeError, ValueError):
        return False


def _verify_configured_password(password: str, entry: dict[str, Any]) -> bool:
    """Accept a preferred hash or a plain password kept in Streamlit Secrets."""
    encoded = str(entry.get("password_hash", "")).strip()
    if encoded:
        return verify_password(password, encoded)

    configured = str(entry.get("password", ""))
    return bool(configured) and hmac.compare_digest(password, configured)


def _secret_section(name: str) -> dict[str, Any]:
    try:
        section = st.secrets.get(name, {})
        return dict(section)
    except FileNotFoundError:
        return {}


def _secret_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _identity_allowed(email: str, config: dict[str, Any]) -> bool:
    email = email.lower().strip()
    allowed_emails = {str(v).lower() for v in config.get("allowed_emails", [])}
    allowed_domains = {str(v).lower() for v in config.get("allowed_domains", [])}
    if not allowed_emails and not allowed_domains:
        return True
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return email in allowed_emails or domain in allowed_domains


def require_user() -> AuthenticatedUser:
    """Authenticate with OIDC, a local password, or explicit demo mode."""
    config = _secret_section("app_auth")
    mode = str(config.get("mode", "password")).lower()

    if mode == "oidc":
        if not st.user.is_logged_in:
            st.title("Costing Tool")
            st.write("Sign in with your company account to continue.")
            if st.button("Sign in", type="primary"):
                st.login()
            st.stop()
        email = str(getattr(st.user, "email", ""))
        name = str(getattr(st.user, "name", email))
        if not _identity_allowed(email, config):
            st.error("This account does not have access to the app.")
            if st.button("Sign out"):
                st.logout()
            st.stop()
        new_item_emails = {
            str(value).lower().strip()
            for value in config.get("new_item_emails", [])
        }
        history_emails = {
            str(value).lower().strip()
            for value in config.get("history_emails", [])
        }
        can_create_new = email.lower().strip() in new_item_emails
        can_view_history = email.lower().strip() in history_emails
        return AuthenticatedUser(
            username=email,
            email=email,
            name=name,
            can_create_new=can_create_new,
            can_view_history=can_view_history,
        )

    if mode == "password":
        if st.session_state.get("authenticated_user"):
            stored = st.session_state.authenticated_user
            return AuthenticatedUser(
                username=str(stored.get("username", stored["email"])),
                email=stored["email"],
                name=stored["name"],
                can_create_new=_secret_bool(stored.get("can_create_new")),
                can_view_history=_secret_bool(stored.get("can_view_history")),
            )

        users = _secret_section("users")
        st.markdown("## Solidus")
        st.title("Spread Costing Tool")
        st.caption("Sign in to continue.")
        if not users:
            st.error(
                "No users are set up. Add a user in Streamlit Secrets."
            )
            st.stop()
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            matched_key = next(
                (key for key in users if key.lower() == username.lower().strip()), None
            )
            entry = dict(users.get(matched_key, {})) if matched_key else {}
            if entry and _verify_configured_password(password, entry):
                can_create_new = _secret_bool(entry.get("can_create_new"))
                can_view_history = _secret_bool(entry.get("can_view_history"))
                st.session_state.authenticated_user = {
                    "username": str(matched_key),
                    "email": str(entry.get("email", matched_key)),
                    "name": str(entry.get("name", matched_key)),
                    "can_create_new": can_create_new,
                    "can_view_history": can_view_history,
                }
                st.rerun()
            st.error("The username or password was not recognised.")
        st.stop()

    if mode == "demo":
        # Demo mode must be explicitly configured and is only for development.
        st.warning("Demo mode: authentication is not enabled.", icon="⚠️")
        return AuthenticatedUser(
            username="demo",
            email="demo@local",
            name="Demo user",
            can_create_new=True,
            can_view_history=True,
        )

    st.error("The login mode in Secrets is not supported.")
    st.stop()


def sign_out_button() -> None:
    config = _secret_section("app_auth")
    mode = str(config.get("mode", "password")).lower()
    if mode == "oidc" and st.sidebar.button("Sign out"):
        st.logout()
    if mode == "password" and st.sidebar.button("Sign out"):
        st.session_state.pop("authenticated_user", None)
        st.rerun()
