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
    email: str
    name: str


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


def _secret_section(name: str) -> dict[str, Any]:
    try:
        section = st.secrets.get(name, {})
        return dict(section)
    except FileNotFoundError:
        return {}


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
    mode = str(config.get("mode", "demo")).lower()

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
            st.error("Your account is not authorised to use this app.")
            if st.button("Sign out"):
                st.logout()
            st.stop()
        return AuthenticatedUser(email=email, name=name)

    if mode == "password":
        if st.session_state.get("authenticated_user"):
            stored = st.session_state.authenticated_user
            return AuthenticatedUser(email=stored["email"], name=stored["name"])

        users = _secret_section("users")
        st.title("Costing Tool")
        with st.form("login_form"):
            username = st.text_input("Email or username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            matched_key = next(
                (key for key in users if key.lower() == username.lower().strip()), None
            )
            entry = dict(users.get(matched_key, {})) if matched_key else {}
            if entry and verify_password(password, str(entry.get("password_hash", ""))):
                st.session_state.authenticated_user = {
                    "email": str(entry.get("email", matched_key)),
                    "name": str(entry.get("name", matched_key)),
                }
                st.rerun()
            st.error("The username or password was not recognised.")
        st.stop()

    # Demo mode is deliberately obvious and must not be used for a live deployment.
    st.warning("Demo mode: authentication is not enabled.", icon="⚠️")
    return AuthenticatedUser(email="demo@local", name="Demo user")


def sign_out_button() -> None:
    config = _secret_section("app_auth")
    mode = str(config.get("mode", "demo")).lower()
    if mode == "oidc" and st.sidebar.button("Sign out"):
        st.logout()
    if mode == "password" and st.sidebar.button("Sign out"):
        st.session_state.pop("authenticated_user", None)
        st.rerun()

