-- 016_conduit_credential.sql
-- PHASE3C · PHASE A · A2-T16 — Conduit's tenant-bearing credential store.
-- (doc-05 §4.5 — authored from the Conduit module doc, NOT doc-02 §2.9.)
--
-- ============================================================================
-- ⚠️  FILE-STAGED — DO NOT APPLY.  THIS MIGRATION IS NOT PART OF THE APPLIED
--     CHAIN.  The migration harness (scripts/apply_migrations.py) MUST EXCLUDE
--     016 (and 015): it asserts the file is present, dry-parses it, and proves
--     the rollback restores cleanly — but it NEVER runs 016 against prod or
--     copy-of-prod as part of 006→014.
--
--     016 depends on enforced RLS (015) and on the org GUC plumbing being
--     proven.  It MUST NOT be applied until AC-RLS-4 is GREEN and conduit_enabled
--     is being turned on (conduit_enabled ships False).  Applied before 015 +
--     the GUC, the credential lookup would either lock out (NULL GUC admits no
--     rows) or, worse, run unscoped — failure-mode #1 (RLS-GUC-leak).  The
--     file-staged status is the guard.
--
--     Naming note: this table is the RENAME of the rejected RDS-4
--     `conduit_api_key`.  The §16.5-canonical name is `conduit_credential`.
--     There is NO separate Conduit billing table — Conduit writes into the
--     single billing_event (migration 011) with event_type='conduit.call'.
-- ============================================================================
--
-- conduit_credential is org-owned and sits in the §16.5 ENFORCED-RLS list.  The
-- RLS predicate is the SAME predicate family as 015 (org GUC OR platform-admin
-- GUC), because enforced RLS is process-agnostic and Conduit MUST set the same
-- app.current_org GUC (via its ConduitTenancyMiddleware SET LOCAL) before any
-- DB access (doc-02 §2.12 / doc-05 §6, the cross-module hard requirement).
--
-- ── BOOTSTRAP CAVEAT (auth bootstrap, doc-05 §4.5) ──────────────────────────
-- API-key resolution happens BEFORE app.current_org is set, because we derive
-- the org FROM the key.  The single lookup
--     SELECT org_id, scopes, status
--       FROM conduit_credential
--      WHERE credential_id = :cid AND status = 'active';
-- therefore runs on a DEDICATED BYPASSRLS resolver connection (used ONLY for
-- this one constrained, one-row query) — or under the platform-admin GUC.
-- Immediately after resolving the org, tenancy.py issues
-- SET LOCAL app.current_org=<org> for the rest of the request, and every
-- subsequent touch is tenant-scoped by p_conduit_credential_tenant.  OAuth
-- tokens carry org_id in the claim, so they need NO pre-tenancy DB lookup.
-- The secret is NEVER stored (credential_id = 'conduit:<sha256(secret)[:12]>',
-- the same identity scheme as observability/keys.py::key_id_for).

BEGIN;

CREATE TABLE conduit_credential (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),   -- stable internal id (invariant)
    org_id          UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    credential_id   TEXT NOT NULL UNIQUE,        -- 'conduit:<sha256(secret)[:12]>' — secret NEVER stored
    kind            TEXT NOT NULL DEFAULT 'api_key',  -- 'api_key' | 'oauth_client' (registered OAuth client ref)
    label           TEXT,                         -- human label for the console
    scopes          TEXT[] NOT NULL DEFAULT '{}', -- snapshot of scopes_for_org(org) at creation/rotation
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'revoked'
    created_by      UUID,                         -- FK-less to "user" (RDS-3 owns it); audit only
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_conduit_credential_cid ON conduit_credential(credential_id);
CREATE INDEX ix_conduit_credential_org ON conduit_credential(org_id, status);

-- RLS (enforced — same predicate family as §2.2 / §16.5 / migration 015).
ALTER TABLE conduit_credential ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_conduit_credential_tenant ON conduit_credential FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

COMMIT;


-- ===========================================================================
-- ROLLBACK — 016_conduit_credential.sql
-- (destructive statements live ONLY in this rollback section; run by hand.)
-- ===========================================================================
-- BEGIN;
--
-- DROP POLICY IF EXISTS p_conduit_credential_tenant ON conduit_credential;
-- DROP INDEX IF EXISTS ix_conduit_credential_org;
-- DROP INDEX IF EXISTS ux_conduit_credential_cid;
-- DROP TABLE IF EXISTS conduit_credential;
--
-- COMMIT;
