from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
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
    is_admin: bool = False
    role: str = "external"
    must_change_password: bool = False
    session_version: int = 1


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


def _role_permissions(role: str) -> tuple[bool, bool]:
    role = str(role or "external").strip().lower()
    return role in {"creator", "admin"}, role == "admin"


def _authenticated_user(username: str, entry: dict[str, Any]) -> AuthenticatedUser:
    role = str(entry.get("role", "")).strip().lower()
    if not role:
        role = (
            "admin"
            if _secret_bool(entry.get("is_admin"))
            else "creator"
            if _secret_bool(entry.get("can_create_new"))
            else "external"
        )
    role_can_create, role_is_admin = _role_permissions(role)
    return AuthenticatedUser(
        username=str(username),
        email=str(entry.get("email", username)),
        name=str(entry.get("name", username)),
        can_create_new=role_can_create or _secret_bool(entry.get("can_create_new")),
        can_view_history=(
            role_is_admin or _secret_bool(entry.get("can_view_history"))
        ),
        is_admin=role_is_admin or _secret_bool(entry.get("is_admin")),
        role=role,
        must_change_password=_secret_bool(entry.get("must_change_password")),
        session_version=int(entry.get("session_version", 1) or 1),
    )


def configured_users_for_import() -> list[dict[str, Any]]:
    """Return Secrets users with passwords converted to one-way hashes."""
    configured: list[dict[str, Any]] = []
    for username, raw_entry in _secret_section("users").items():
        entry = dict(raw_entry)
        encoded = str(entry.get("password_hash", "")).strip()
        valid_encoded = encoded.startswith("pbkdf2_sha256$")
        imported_from_plain_password = False
        if not valid_encoded:
            plain = str(entry.get("password", ""))
            if not plain:
                continue
            encoded = make_password_hash(plain)
            imported_from_plain_password = True
        user = _authenticated_user(str(username), entry)
        configured.append(
            {
                "username": user.username,
                "email": user.email,
                "name": user.name,
                "password_hash": encoded,
                "role": user.role,
                "can_view_history": user.can_view_history,
                "must_change_password": imported_from_plain_password,
            }
        )
    return configured


def _identity_allowed(email: str, config: dict[str, Any]) -> bool:
    email = email.lower().strip()
    allowed_emails = {str(v).lower() for v in config.get("allowed_emails", [])}
    allowed_domains = {str(v).lower() for v in config.get("allowed_domains", [])}
    if not allowed_emails and not allowed_domains:
        return True
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return email in allowed_emails or domain in allowed_domains


def session_timeout_minutes() -> int:
    """Return the configured inactivity limit, with a safe minimum."""
    configured = _secret_section("app_auth").get("session_timeout_minutes", 60)
    try:
        return max(5, int(configured))
    except (TypeError, ValueError):
        return 60


def authenticate_admin(
    username: str,
    password: str,
    repository: Any | None = None,
) -> AuthenticatedUser | None:
    """Check an administrator's password without changing the signed-in user."""
    config = _secret_section("app_auth")
    if str(config.get("mode", "password")).lower() != "password":
        return None
    if repository is not None and repository.has_app_users():
        entry = repository.get_app_user(username)
        if (
            not entry
            or not bool(entry.get("is_active"))
            or str(entry.get("role", "")).lower() != "admin"
            or not verify_password(password, str(entry.get("password_hash", "")))
        ):
            return None
        return _authenticated_user(str(entry["username"]), dict(entry))

    users = _secret_section("users")
    matched_key = next(
        (
            key
            for key in users
            if str(key).lower() == str(username).lower().strip()
        ),
        None,
    )
    entry = dict(users.get(matched_key, {})) if matched_key else {}
    if (
        not entry
        or not _secret_bool(entry.get("is_admin"))
        or not _verify_configured_password(password, entry)
    ):
        return None
    return _authenticated_user(str(matched_key), entry)


def require_user(repository: Any | None = None) -> AuthenticatedUser:
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
        admin_emails = {
            str(value).lower().strip()
            for value in config.get("admin_emails", [])
        }
        can_create_new = email.lower().strip() in new_item_emails
        can_view_history = email.lower().strip() in history_emails
        return AuthenticatedUser(
            username=email,
            email=email,
            name=name,
            can_create_new=can_create_new,
            can_view_history=can_view_history,
            is_admin=email.lower().strip() in admin_emails,
        )

    if mode == "password":
        if st.session_state.get("authenticated_user"):
            stored = st.session_state.authenticated_user
            username = str(stored.get("username", stored["email"]))
            database_backed = stored.get("database_backed")
            if database_backed is None:
                database_backed = bool(
                    repository is not None and repository.has_app_users()
                )
                stored["database_backed"] = database_backed
            last_check = float(stored.get("last_auth_check_monotonic", 0) or 0)
            check_due = time.monotonic() - last_check >= 60
            if repository is not None and database_backed and check_due:
                entry = repository.get_app_user(username)
                if (
                    not entry
                    or not bool(entry.get("is_active"))
                    or int(entry.get("session_version", 1) or 1)
                    != int(stored.get("session_version", 1) or 1)
                ):
                    st.session_state.clear()
                    st.session_state.login_notice = (
                        "Your account or session has changed. Sign in again."
                    )
                    st.rerun()
                user = _authenticated_user(str(entry["username"]), dict(entry))
                st.session_state.authenticated_user.update(
                    {
                        "email": user.email,
                        "name": user.name,
                        "can_create_new": user.can_create_new,
                        "can_view_history": user.can_view_history,
                        "is_admin": user.is_admin,
                        "role": user.role,
                        "must_change_password": user.must_change_password,
                        "session_version": user.session_version,
                        "database_backed": True,
                        "last_auth_check_monotonic": time.monotonic(),
                    }
                )
                return user
            return _authenticated_user(username, dict(stored))

        users = _secret_section("users")
        database_users = bool(repository is not None and repository.has_app_users())
        login_notice = st.session_state.pop("login_notice", None)
        st.markdown("## Solidus")
        st.title("Spread Costing Tool")
        st.caption("Sign in to continue.")
        if login_notice:
            st.info(login_notice)
        if not users and not database_users:
            st.error(
                "No users are set up. Ask an administrator to add your account."
            )
            st.stop()
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            matched_key = None
            entry: dict[str, Any] = {}
            valid = False
            if database_users:
                database_entry = repository.get_app_user(username)
                if database_entry:
                    entry = dict(database_entry)
                    matched_key = str(entry["username"])
                    valid = bool(entry.get("is_active")) and verify_password(
                        password, str(entry.get("password_hash", ""))
                    )
            else:
                matched_key = next(
                    (
                        key
                        for key in users
                        if key.lower() == username.lower().strip()
                    ),
                    None,
                )
                entry = dict(users.get(matched_key, {})) if matched_key else {}
                valid = bool(entry) and _verify_configured_password(password, entry)
            if valid and matched_key:
                user = _authenticated_user(str(matched_key), entry)
                st.session_state.authenticated_user = {
                    "username": user.username,
                    "email": user.email,
                    "name": user.name,
                    "can_create_new": user.can_create_new,
                    "can_view_history": user.can_view_history,
                    "is_admin": user.is_admin,
                    "role": user.role,
                    "must_change_password": user.must_change_password,
                    "session_version": user.session_version,
                    "database_backed": database_users,
                    "last_auth_check_monotonic": time.monotonic(),
                }
                if database_users:
                    repository.record_user_login(user.username)
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
            is_admin=True,
        )

    st.error("The login mode in Secrets is not supported.")
    st.stop()


def sign_out_button(repository: Any | None = None) -> None:
    config = _secret_section("app_auth")
    mode = str(config.get("mode", "password")).lower()
    if mode == "oidc" and st.sidebar.button("Sign out"):
        if repository is not None:
            repository.end_session(st.session_state.get("app_session_id", ""))
        st.logout()
    if mode == "password" and st.sidebar.button("Sign out"):
        if repository is not None:
            repository.end_session(st.session_state.get("app_session_id", ""))
        st.session_state.pop("authenticated_user", None)
        st.session_state.pop("app_session_id", None)
        st.session_state.pop("app_signed_in_at", None)
        st.session_state.pop("app_last_activity_at", None)
        st.session_state.pop("app_active_seconds", None)
        st.rerun()
