-- Solidus Costing Tool database schema.
-- Run as the Neon database owner on a new project before switching Streamlit.

CREATE TABLE IF NOT EXISTS public.costing_revisions (
    costing_id text PRIMARY KEY,
    item_code text NOT NULL,
    revision integer NOT NULL,
    source_item_code text,
    customer_name text,
    quote_reference text,
    created_at_utc timestamptz NOT NULL DEFAULT now(),
    created_by_email text NOT NULL,
    created_by_username text NOT NULL,
    created_by_name text,
    record jsonb NOT NULL,
    UNIQUE (item_code, revision)
);

CREATE INDEX IF NOT EXISTS costing_revisions_created_at_idx
    ON public.costing_revisions (created_at_utc DESC);
CREATE INDEX IF NOT EXISTS costing_revisions_created_by_email_created_at_idx
    ON public.costing_revisions (created_by_email, created_at_utc DESC);
CREATE INDEX IF NOT EXISTS costing_revisions_item_code_created_at_idx
    ON public.costing_revisions (item_code, created_at_utc DESC);
CREATE UNIQUE INDEX IF NOT EXISTS costing_revisions_quote_reference_unique_idx
    ON public.costing_revisions (quote_reference)
    WHERE quote_reference IS NOT NULL AND btrim(quote_reference) <> '';

CREATE TABLE IF NOT EXISTS public.app_sessions (
    session_id text PRIMARY KEY,
    username text NOT NULL,
    name text,
    email text NOT NULL,
    signed_in_at_utc timestamptz NOT NULL,
    last_activity_utc timestamptz NOT NULL,
    last_heartbeat_utc timestamptz NOT NULL,
    active_seconds numeric NOT NULL DEFAULT 0,
    current_page text,
    force_logout boolean NOT NULL DEFAULT false,
    ended_at_utc timestamptz
);

CREATE INDEX IF NOT EXISTS app_sessions_open_idx
    ON public.app_sessions (last_heartbeat_utc DESC)
    WHERE ended_at_utc IS NULL;
CREATE INDEX IF NOT EXISTS app_sessions_username_last_activity_idx
    ON public.app_sessions (username, last_activity_utc DESC);

CREATE TABLE IF NOT EXISTS public.app_users (
    username text PRIMARY KEY,
    email text NOT NULL,
    name text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'external',
    can_view_history boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    must_change_password boolean NOT NULL DEFAULT true,
    session_version integer NOT NULL DEFAULT 1,
    created_at_utc timestamptz NOT NULL DEFAULT now(),
    updated_at_utc timestamptz NOT NULL DEFAULT now(),
    password_changed_at_utc timestamptz,
    last_login_at_utc timestamptz,
    created_by text,
    CONSTRAINT app_users_role_check
        CHECK (role IN ('external', 'creator', 'admin'))
);

CREATE UNIQUE INDEX IF NOT EXISTS app_users_username_lower_idx
    ON public.app_users (lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS app_users_email_lower_idx
    ON public.app_users (lower(email));
CREATE INDEX IF NOT EXISTS app_users_active_role_idx
    ON public.app_users (is_active, role);

CREATE TABLE IF NOT EXISTS public.user_signatures (
    signature_id text PRIMARY KEY,
    username text NOT NULL REFERENCES public.app_users(username)
        ON UPDATE CASCADE ON DELETE CASCADE,
    image_png bytea NOT NULL,
    image_sha256 text NOT NULL,
    created_at_utc timestamptz NOT NULL DEFAULT now(),
    revoked_at_utc timestamptz,
    created_by text NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS user_signatures_one_active_per_user_idx
    ON public.user_signatures (lower(username))
    WHERE revoked_at_utc IS NULL;
CREATE INDEX IF NOT EXISTS user_signatures_username_created_idx
    ON public.user_signatures (lower(username), created_at_utc DESC);

CREATE TABLE IF NOT EXISTS public.app_audit_log (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at_utc timestamptz NOT NULL DEFAULT now(),
    actor_username text NOT NULL,
    action text NOT NULL,
    target_username text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS app_audit_log_occurred_idx
    ON public.app_audit_log (occurred_at_utc DESC);
CREATE INDEX IF NOT EXISTS app_audit_log_target_idx
    ON public.app_audit_log (lower(target_username), occurred_at_utc DESC);

CREATE TABLE IF NOT EXISTS public.commercial_approval_requests (
    request_id text PRIMARY KEY,
    approval_basis text NOT NULL,
    requester_username text NOT NULL,
    requester_name text NOT NULL,
    requester_email text NOT NULL,
    item_code text NOT NULL,
    customer_name text NOT NULL,
    request_reason text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    requested_at_utc timestamptz NOT NULL DEFAULT now(),
    decided_at_utc timestamptz,
    decided_by_username text,
    decided_by_name text,
    decided_by_email text,
    decision_reason text,
    snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT commercial_approval_status_check
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS commercial_approval_pending_idx
    ON public.commercial_approval_requests (status, requested_at_utc DESC);
CREATE INDEX IF NOT EXISTS commercial_approval_requester_idx
    ON public.commercial_approval_requests
    (lower(requester_username), approval_basis, requested_at_utc DESC);

GRANT SELECT, INSERT, UPDATE ON TABLE public.commercial_approval_requests
    TO costing_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.user_signatures TO costing_app;
