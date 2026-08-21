-- One-time production migration for personal sales-representative signatures.
-- Run in Neon SQL Editor as the database owner on the production branch.

BEGIN;

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

GRANT SELECT, INSERT, UPDATE ON TABLE public.user_signatures TO costing_app;

COMMIT;
