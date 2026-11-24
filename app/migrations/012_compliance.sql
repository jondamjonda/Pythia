-- 012_compliance.sql
-- PHASE3C · Phase A · A2-T12 / WP-012 — compliance fields + handoff + disclaimer (INERT).
--
-- This migration is purely ADDITIVE and changes NO behaviour. It opens the seams
-- the compliance lane (the "information vs reserved-advice" answer-mode split, the
-- consent-gated lawyer handoff, and the versioned disclaimer registry) will later
-- use. Nothing here is wired into the cascade yet — the application keeps emitting
-- findings exactly as before (answer_mode defaults to 'information'), and the new
-- handoff/disclaimer tables stay empty until Phase B turns the guard on behind a
-- flag (compliance_guard_mode=shadow is the safe default).
--
-- Source DDL: 02_Module_Polarius_Architecture_v5.md §2.8 (authoritative table/column
-- specs). §16/§8 reconciliations win where they differ.
--
-- Three things this migration deliberately RELIES ON (re-verified, not changed):
--   1. finding.finding_type is plain TEXT with NO CHECK constraint (see 001:224) —
--      so a 'refusal' finding is insertable WITHOUT any constraint edit here.
--   2. The cited_provisions_nonempty / cited_documents_nonempty CHECKs (001:251-254)
--      are unconditional on finding_type, so they ALSO fire on refusal findings —
--      a refusal must still cite what it refused on. Not touched here.
--   3. The EmptyCitationError before_insert / before_flush hooks (storage/audit.py:
--      72-91) are likewise finding_type-agnostic and keep firing on refusals.
--
-- RLS note: handoff is tenant-scoped (§16.5 — RLS ENFORCED later in the file-staged
-- migration 015, not here, to keep 012 inert). disclaimer is shared reference data
-- (§2.10) → PUBLIC, never RLS'd.

BEGIN;

-- ============================================================================
-- AMEND finding — four compliance columns, all additive + defaulted (§2.8).
-- ----------------------------------------------------------------------------
-- answer_mode:           the lane this finding was answered in. Defaults to
--                        'information' so every pre-existing and newly-inserted
--                        row is unchanged in meaning until the guard is wired.
-- compliance_reason:     human-readable why (e.g. why it was reserved/needs-review).
-- compliance_decided_by: provenance of the decision ('rule:advice-guard-v1' |
--                        'gpt-4o-mini:advice-guard-v1').
-- disclaimer_id:         stable id of the disclaimer version applied (FK-less TEXT
--                        link to disclaimer.id; nullable until the guard sets it).
-- ============================================================================
ALTER TABLE finding
    ADD COLUMN IF NOT EXISTS answer_mode           TEXT NOT NULL DEFAULT 'information',
    ADD COLUMN IF NOT EXISTS compliance_reason     TEXT,
    ADD COLUMN IF NOT EXISTS compliance_decided_by TEXT,
    ADD COLUMN IF NOT EXISTS disclaimer_id         TEXT;
-- finding_type has no CHECK today (001:224 is plain TEXT) → 'refusal' is allowed
-- without a constraint edit. cited_provisions_nonempty / cited_documents_nonempty
-- (001:251-254) are preserved and still apply to refusal findings.


-- ============================================================================
-- NEW handoff — lawyer-referral record (RDS-2 §2.4).
-- ----------------------------------------------------------------------------
-- PII discipline: contact_payload carries personal data ONLY after consent_at is
-- set (GDPR — §4.2 failure-mode 7 "PII-in-handoff"). It defaults to '{}' (empty)
-- so a row created before consent holds no PII. consent_at records WHEN the user
-- consented to share. The application must not write contact_payload until
-- consent_at is non-null; this table only provides the seam (the column ordering
-- here makes that invariant legible).
--
-- org_id is a real FK under the canonical chain (006 precedes 012). ON DELETE
-- SET NULL keeps the referral row even if the org is removed.
-- Tenant-scoped: §16.5 RLS is ENFORCED in the file-staged migration 015.
-- ============================================================================
CREATE TABLE IF NOT EXISTS handoff (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id       UUID REFERENCES project(id) ON DELETE SET NULL,       -- nullable: input-gate refusals have no project
    org_id           UUID REFERENCES organization(id) ON DELETE SET NULL,
    trigger          TEXT NOT NULL,                                        -- 'reserved_advice'|'below_confidence'|'user_requested'
    answer_mode      TEXT NOT NULL,
    reason           TEXT NOT NULL,
    source_query     TEXT,                                                 -- truncated; for the lawyer's context
    cited_provisions UUID[] NOT NULL DEFAULT '{}',                         -- provisions the system DID surface (info lane)
    status           TEXT NOT NULL DEFAULT 'offered',                      -- 'offered'->'accepted'->'sent'->'closed'|'declined'
    partner_id       TEXT,
    partner_ref      TEXT,                                                 -- external referral id; never PK
    contact_payload  JSONB NOT NULL DEFAULT '{}'::jsonb,                   -- PII ONLY AFTER consent_at is set
    consent_at       TIMESTAMPTZ,                                          -- GDPR: when the user consented to share PII
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_handoff_project ON handoff(project_id);
CREATE INDEX IF NOT EXISTS ix_handoff_org     ON handoff(org_id, created_at);
CREATE INDEX IF NOT EXISTS ix_handoff_status  ON handoff(status, created_at);


-- ============================================================================
-- NEW disclaimer — versioned, provable disclaimer registry (binder §3 → a TABLE).
-- ----------------------------------------------------------------------------
-- Shared reference data → PUBLIC, NOT RLS'd (§2.10). The code constant in
-- analysis/disclaimers.py seeds it and acts as the in-process cache. A finding
-- stores only disclaimer_id, so given a finding row alone (offline) one can
-- reconstruct which disclaimer + answer_mode + model + prompt_hash applied
-- (the AC-20 provability requirement). TEXT primary key = the stable id.
-- ============================================================================
CREATE TABLE IF NOT EXISTS disclaimer (
    id             TEXT PRIMARY KEY,                  -- stable id, e.g. 'info.v1'
    answer_mode    TEXT NOT NULL,                     -- 'information'|'reserved_advice'|'needs_review'
    confidence     TEXT NOT NULL DEFAULT 'normal',    -- 'normal'|'low'
    text_cs        TEXT NOT NULL,
    text_en        TEXT NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Seed ids live in analysis/disclaimers.py:
--   'info.v1', 'info.low_confidence.v1', 'refusal.reserved_advice.v1'
-- (the code constant is the source of the text_cs/text_en bodies). No seed rows
-- are inserted here — seeding is the application's job so 012 stays inert.

COMMIT;


-- ============================================================================
-- ROLLBACK (012_compliance.sql)
-- ----------------------------------------------------------------------------
-- Destructive statements are confined to this section, per the additive rule.
-- Apply manually to undo 012. Dropping handoff/disclaimer loses any rows the
-- application may have written; the four finding columns are dropped last.
-- ============================================================================
-- BEGIN;
--
-- DROP TABLE IF EXISTS disclaimer;
-- DROP TABLE IF EXISTS handoff;
--
-- ALTER TABLE finding
--     DROP COLUMN IF EXISTS disclaimer_id,
--     DROP COLUMN IF EXISTS compliance_decided_by,
--     DROP COLUMN IF EXISTS compliance_reason,
--     DROP COLUMN IF EXISTS answer_mode;
--
-- COMMIT;
