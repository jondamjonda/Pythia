"""Application settings and feature flags, loaded from the environment and env files."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Strip empty-string optional env vars before pydantic reads them. Windows
# sometimes leaves entries like `ANTHROPIC_API_KEY=` (empty) in the user
# environment; without this, the empty OS var would shadow secrets.env.
for _k in (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "COHERE_API_KEY",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "HETZNER_HOST",
    "HETZNER_SSH_KEY",
):
    if os.environ.get(_k) == "":
        del os.environ[_k]


class Settings(BaseSettings):
    """Application settings, read from environment variables, secrets.env and .env.

    Field names map to environment variables case-insensitively (``pythia_url`` reads
    ``PYTHIA_URL``). Every feature flag defaults to its safe setting.
    """

    model_config = SettingsConfigDict(
        env_file=("secrets.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(
        default="postgresql+asyncpg://regsuper:regsuper@localhost:5432/regsuper",
        alias="DATABASE_URL",
    )
    database_url_sync: str = Field(
        default="postgresql://regsuper:regsuper@localhost:5432/regsuper",
        alias="DATABASE_URL_SYNC",
    )

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    cohere_api_key: str | None = None

    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "regsuper-mvp"
    s3_region: str = "fsn1"

    log_dir: Path = Path("./logs")
    data_dir: Path = Path("./data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    admin_basic_auth_user: str = "admin"
    admin_basic_auth_pass: str = "changeme"

    # Fallback for the retrieval refusal gate. The live value is the calibrated threshold that
    # calibration/runtime.py persists (fitted from labelled retrieval_eval rows) and loads at
    # request time; see get_refusal_threshold there. This 0.6 applies only while no calibration
    # has been published, or when the persisted file is corrupt. Keep it conservative: it is the
    # floor that stops low-confidence retrieval from passing before the system is calibrated.
    refusal_threshold: float = 0.6

    # Per-role chat model selection, including the cheap model, lives in
    # analysis/model_resolver.py (an environment override first, then a hard-coded default).
    # Do not add chat-model keys here.
    embedding_model: str = "text-embedding-3-large"
    rerank_model: str = "rerank-v3.5"

    # Vector dim must match VECTOR(N) columns in the schema.
    embedding_dim: int = 1536

    # Pythia engine sidecar, off by default. When enabled and pythia_url is set, the
    # /api/pythia/* endpoints proxy legal questions to the engine over HTTP. When disabled, the
    # app uses only its own OpenAI-based L2/L3 analysis path.
    pythia_enabled: bool = False
    pythia_url: str | None = None

    # Put the engine /v1/answer call behind the same pre-call kill switch and spend limit
    # (limits.enforce_spend_limit) that guard direct OpenAI calls, checked against the
    # 'pythia_engine_llm' provider caps. Off by default because the shipped data/limits.json carries
    # a $1/day test cap that would refuse local answers immediately: set realistic caps first, then
    # enable this. Only answer() is gated; retrieve(), candidates() and browse() make no LLM calls.
    pythia_boundary_spend_gate_enabled: bool = False

    # Which corpus the app reasons over. "regsuper" (default) uses the app's own provision tables.
    # "nexus" reads the canonical Nexus corpus through the Pythia sidecar (/v1/candidates,
    # /v1/provisions) and requires pythia_enabled and pythia_url. It can be switched back at any
    # time; the analysis cascade accepts "nexus" only once cutover_approved is also set.
    law_source: Literal["regsuper", "nexus"] = "regsuper"

    # ── Control room ──────────────────────────────────────────────────────────────────────────────
    # If set, the /admin/api/* control surface requires a matching X-Control-Token header (the
    # Next.js server proxy forwards it). If unset, local environments need no token (the loopback
    # bind is the boundary); elsewhere the gate stays closed unless control_room_open_when_unset.
    control_panel_token: str | None = None
    # Read-only database URL for the Nexus `pythia` database, used for per-database storage and
    # embedding statistics on the System panel. Unset means those metrics report as unavailable,
    # never as a fake zero.
    pythia_database_url: str | None = None
    # System-stats collector settings.
    system_sample_interval_s: float = 5.0  # collector base tick
    system_ring_seconds: int = 3600  # live window kept in memory (1 h at 5 s = 720 samples)
    system_persist_every: int = 12  # every Nth tick, down-sample one scalar row into system_sample
    # Not read by any code. The runtime resource watchdog is toggled by
    # resource_caps.watchdog_enabled in limits.json (consumed by resource_guard.py and exposed
    # through the admin limits endpoint); setting this field to False does not disable it.
    watchdog_enabled: bool = (
        True  # not read; the live watchdog toggle is resource_caps.watchdog_enabled in limits.json
    )

    # ── Feature flags ─────────────────────────────────────────────────────────────────────────────
    # Each flag below gates one capability and defaults to its safe setting, so code can be merged
    # before it is switched on. A flag that enforces something is switched on only after the check
    # that proves it correct has passed.

    # Multi-tenant isolation with Postgres row-level security. Both off means single-tenant
    # behaviour. rls_enforced is switched on only after the tenant backfill and a cross-tenant
    # leak probe have passed.
    tenancy_enabled: bool = False
    rls_enforced: bool = False

    # Plan entitlements and metering: per-tier scopes, quotas and analysis-depth clamping
    # (entitlements/guard.py, entitlements/metering.py). When off, routes run unguarded and
    # unmetered. billing_provider is the no-op billing event outbox; "noop" is the only value.
    entitlements_enabled: bool = False
    billing_provider: Literal["noop"] = "noop"

    # Compliance lane: legal information versus legal advice, as the Czech Bar Association (ČAK)
    # distinguishes them. The guard is on, in shadow mode: it runs and logs its verdict but never
    # blocks. Only mode='enforce' guarantees the lane, and it is enabled only after lawyer sign-off
    # and a passing fail-closed acceptance test.
    compliance_guard_enabled: bool = True
    compliance_guard_mode: Literal["shadow", "enforce"] = "shadow"

    # L4 gap-analysis mode. No runtime code reads it: L4 is always the keyword stub (l4_gaps.py
    # stamps generated_by="rule:l4-keyword-stub-v1") and the grounded LLM path is not built. The
    # local run scripts export L4_MODE=llm, which has no effect either. 'llm' becomes meaningful
    # only once that path is built and reaches at least 80% gap precision.
    l4_mode: Literal["stub", "llm"] = "stub"

    # Per-domain corpus cutover list. Not read by any code; the live cutover is controlled by
    # law_source, pythia_enabled and cutover_approved.
    cutover_domains: list[str] = Field(default_factory=list)

    # Operator attestation that the human cutover gates have passed: a lawyer reviewed a sample of
    # at least 50 nexus findings as faithful, and the refusal-floor fallback was accepted. The live
    # cascade refuses law_source='nexus' unless both law_source='nexus' and cutover_approved=True
    # (require_cascade_law_source); neither flag alone activates the cutover. The Pythia engine must
    # be reachable (pythia_enabled and pythia_url), otherwise the nexus L1/L2 reads fail. Coverage
    # note: a nexus finding that cites an act outside the regsuper-to-nexus crosswalk (currently 11
    # acts) is dropped.
    cutover_approved: bool = False

    # Conduit: the metered MCP/REST distribution service, run as a separate process.
    # create_conduit_app refuses to start while conduit_enabled is False; this never affects the
    # main API. corpus_scope records the Conduit corpus boundary (legislation and obligations only;
    # case law is deliberately out of scope) and is not read by code. Caller query text is not
    # persisted unless conduit_capture_query_text is True.
    conduit_enabled: bool = False
    conduit_capture_query_text: bool = False
    corpus_scope: Literal["legislation_obligations"] = "legislation_obligations"
    conduit_oauth_issuer_url: str | None = None  # placeholders for the OAuth and pricing build
    conduit_oauth_audience: str | None = None
    conduit_resource_uri: str | None = None
    conduit_unit_price_usd: float | None = None
    conduit_token_ttl_s: int | None = None

    # Run the analysis cascade as a Celery task. When off, the cascade runs synchronously inside
    # the request. When on, the same body runs inline through Celery's eager mode (no broker);
    # dispatching to a real broker is not built yet.
    analyze_async: bool = False

    # Security, admin, accounting and tuning switches; all default to closed or off.
    require_auth: bool = Field(
        default=False, alias="REQUIRE_AUTH"
    )  # when True, product auth fails closed
    # Not read by any code, and there is no /internal/emit route. The /internal routes are guarded
    # by the environment setting and the control token instead (see api/main.py).
    internal_secret: str | None = None
    control_room_open_when_unset: bool = (
        False  # if False, the admin gate stays closed outside local when no token is set
    )
    replay_enabled: bool = False  # gates the live-replay admin endpoint
    accounting_integration_enabled: bool = False  # POHODA / Money S3 import
    # GDPR project deletion is not built yet; no code reads this flag.
    project_deletion_enabled: bool = False
    # Fallback floor for refusals inside the analysis cascade. calibration/runtime.py prefers a
    # persisted cascade_applicability_floor, but nothing writes that key yet, so this 0.5 applies.
    # Separate from refusal_threshold (the retrieval gate above).
    cascade_refusal_floor: float = 0.5
    limits_low_ok: bool = (
        False  # if False, startup warns about suspiciously low limits.json caps ($1/$1 test caps)
    )

    # EU AI Act audit export and public trust pages. The reproducible export (canonical JSON plus
    # SHA-256, content-only so re-exporting the same analysis yields the same bytes) is the
    # legal-defensibility artifact; the trust routes are the Article 13 transparency surface.
    # Whether the system is high-risk under the AI Act is a human legal decision, stored as
    # authored text in ai_act_high_risk_classification and never computed in code.
    audit_export_enabled: bool = (
        False  # if False, the export routes return 404 and no audit export is served
    )
    # Not read by any code: no PDF renderer is installed, so PDF export is deferred. The JSON plus
    # SHA-256 export is the authoritative artifact.
    export_pdf_enabled: bool = False
    data_residency_compute_region: str = (
        "EU"  # surfaced in /api/trust/data-residency and every export envelope
    )
    data_residency_storage_region: str = "EU"
    openai_dpa_url: str = "https://openai.com/policies/data-processing-addendum"
    operator_contact_email: str = (
        ""  # surfaced in the AI Act technical doc; empty until the operator sets it
    )
    ai_act_high_risk_classification: str = (  # authored by the operator, not computed
        "Self-assessed: not high-risk under EU AI Act Annex III — a general legal-information tool, "
        "not a listed high-risk use. A formal conformity assessment is pending legal review before "
        "2 August 2026. This determination is authored by the operator/legal team, not auto-computed."
    )

    # Reproducible evidence pack: a read-only legal-evidence bundle for one analysis, switched
    # independently of audit_export_enabled. It holds the canonical analysis export (grounded cited
    # provisions), a point-in-time proof (each cited provision's [valid_from, valid_to) window
    # against the analysis as-of date), the refusal and grounding trail, and the append-only audit
    # trail, all addressed by one SHA-256. It reuses export.build_canonical_payload, so the
    # embedded export hashes identically to a standalone one. When False, POST
    # /api/evidence-pack/* returns 404; when True the route makes no database or disk writes.
    audit_evidence_enabled: bool = False

    # SME business-profile CRUD and plain-language digest endpoints. The routers are always
    # mounted, but every route returns 404 until its flag is on. profiles_enabled gates profile
    # CRUD and profile-driven analysis; digest_enabled gates the read-only digest views.
    profiles_enabled: bool = False
    digest_enabled: bool = False

    # The app's answer route, POST /api/answer; it returns 404 until enabled. When on, it calls the
    # engine answer pipeline through pythia.client.answer() and shapes a grounded, cited
    # AnswerResponse (sections, chips, meta, band). This flag only exposes the route: what the
    # engine returns is governed by the engine's own metagate_enabled, metagate_mode and
    # synthesizer_provider settings. Live answers also need pythia_enabled and a reachable
    # pythia_url; quota enforcement follows entitlements_enabled. Production copy and any paid
    # launch wait on lawyer sign-off.
    answer_endpoint_enabled: bool = False

    # Show hedged answers for weak corpus coverage while the engine's metagate runs in shadow mode.
    # In shadow mode the engine classifies and logs but still drafts, so it can return a grounded,
    # self-hedged draft for a not_in_corpus route whose coverage is only weak (related material,
    # not an exact match; AML or GDPR questions, for example). With this flag off the app refuses
    # such an answer and shows the closest provisions; with it on, the app shows the draft as a
    # hedged answer (sections, chips and the hedge banner). The scope is deliberately narrow to keep
    # the anti-hallucination guard intact: it applies only to shadow mode with
    # coverage=='weak_coverage', no engine refusal and verdict=='ok'. no_coverage (drafts would cite
    # unrelated law), not_a_legal_question, false_premise and every engine refusal stay refused.
    # This is not an enforce-mode switch; in enforce mode the engine stops before drafting.
    answer_shadow_weak_coverage_hedged: bool = False

    # Pass the saved business profile into the /api/answer intake. When off, the handler never loads
    # a profile or builds an AnswerContext, and never sends project_context to the engine. When on
    # and the request carries a project_id, the handler loads the organisation-scoped project's
    # BusinessProfile, builds AnswerContext.from_business_profile and sends a small whitelisted
    # typed dict as project_context (legal_form, nace_code, derived_tags, size bands, jurisdiction,
    # the five gate booleans, municipality_code, preferred_language; no free text or personal
    # data), so known profile facts seed or override intake extraction. The dict is never persisted
    # into api_call.request_json (cost rows stay {}). It seeds retrieval and intake only; profile
    # facts never reach the cascade, candidate or verdict logic, which stays lawyer-gated.
    answer_profile_context_enabled: bool = False

    # Deterministic clarifying question on /api/answer, asked before any quota is reserved. When on,
    # if the detected matter type has a required slot that is still missing (not pinned by the
    # profile AnswerContext, by resubmitted typed slots, or by a regex/gazetteer read of the query)
    # and the request has no `slots` field (the first turn), the handler returns a 200 clarify frame
    # (render_mode='clarify', up to three fixed Czech draft questions, the information-not-advice
    # disclaimer and a professional-referral line) without reserving quota or calling the engine:
    # no LLM call and no spend. Asking is cheaper and more honest than answering a too-thin
    # question wrongly. There is at most one round: a follow-up that carries `slots` always proceeds
    # to quota and the engine, which answers hedged if facts are still missing. The check runs after
    # the input compliance gate, so a reserved-advice refusal in enforce mode still comes first (a
    # lawyer handoff, not a clarify prompt). It runs before the engine call, so it cannot collide
    # with the post-engine weak-coverage hedge. An engine-side clarify based on retrieval strength
    # is a separate, later step and is not wired here. MATTER_REQUIRED_SLOTS, the keyword lexicon
    # and the Czech question copy are drafts awaiting lawyer review before this flag is enabled.
    answer_clarify_enabled: bool = False

    # Surface the engine's structured practical block on /api/answer. When off, the handler never
    # reads the engine's `.practical` key and AnswerResponse.practical stays None. When on and the
    # engine sends a consistent `.practical` block (it does so only under its own
    # structured_answer_enabled flag), the app extends the citation check to the block: each
    # PracticalItem.provision_id must be in the grounded cited-provision set, otherwise the item is
    # dropped and its slot recorded as demoted. Gaps and demoted slots pass through as honest
    # non-claims, and each gap emits answer.practical.gap telemetry (gap kind and frequency feed the
    # connector backlog). No new data store. The block is plain data the frontend may render; the
    # flat sections and chips remain the complete answer.
    answer_practical_block_enabled: bool = False

    # The understanding panel: show how the engine understood a lay question. When off, the handler
    # never reads audit_trace["intake"]; AnswerResponse.intake stays None and profile_suggestions
    # empty. When on and the engine attaches audit_trace["intake"] (sub-questions, extracted
    # closed-enum facts, conflicts, sufficiency, and which proposed facts were dropped and why), the
    # app returns a whitelisted typed intake block (sub_questions, facts[{field, value, source}],
    # conflicts, sufficiency, dropped[{field, reason}]) plus profile_suggestions: the asker's own
    # whitelisted facts, offered for a one-click, consented write-back through the profiles CRUD
    # and never written automatically. The artifact is never persisted into api_call.request_json.
    answer_intake_panel_enabled: bool = False

    # Spend-cap and kill-switch precheck inside the answer handler. When on, right after reserving
    # the quota unit and before the sidecar call, the handler runs assert_not_killed() and
    # enforce_spend_limit(provider='pythia_engine_llm', projected_cost_usd=<worst case>). On
    # SpendLimitExceeded or KillSwitchEngaged it returns a structured refusal (an AnswerResponse,
    # not a 500) and refunds the reserved quota unit idempotently, so a capped request never
    # reaches the engine and costs no quota. This complements the boundary gate in pythia.client
    # (pythia_boundary_spend_gate_enabled): stopping in the handler yields a clean refusal frame
    # instead of the client's 503. Order, enforced by tests: entitlement, input gate, clarify,
    # quota reserve, precheck, sidecar call. A 429 from the quota reserve therefore wins.
    answer_budget_precheck_enabled: bool = False

    # After an answer whose claim-verification loop ran, write a canonical-JSON audit bundle of the
    # claim ledger, plus its SHA-256, to data/exports/claim_ledgers/. The write is a best-effort
    # background task (asyncio.create_task, errors logged and swallowed), so it never delays or
    # denies the answer. The bundle holds the ledger, per-stage llm_usage counts, the engine
    # request_id, the corpus snapshot id, the app flag manifest, model ids and prompt hashes. The
    # asker's query text is included only when conduit_capture_query_text is True; otherwise only
    # counts, ids and hashes are written. The canonical JSON is byte-stable (re-serialising the same
    # content reproduces the file), so the recomputed SHA-256 matches the sidecar file.
    claim_ledger_export_enabled: bool = False

    # Entity and role applicability ("does this bind you?"). When off, the post-L3 scoring pass
    # never runs, no payload['applicability'] key is written, intake and profile never write
    # metadata_['entity_role'], and render and export output is unchanged (the key is emitted only
    # if present). When on, it adds a per-obligation binds verdict (yes, conditional or
    # out_of_scope), computed deterministically from the already-extracted actor with no LLM call
    # or prompt change, and a "does not bind you directly" render section. It demotes findings and
    # never drops them; suppressing L4 gaps is a separate switch (see the gap-suppression flags
    # below). entity_scoping_floor is the binds confidence below which out_of_scope is softened to
    # conditional (0.0 disables this; the value is set by a lawyer).
    entity_scoping_enabled: bool = False
    entity_scoping_floor: float = 0.0

    # Applicability-confidence split. Requires entity_scoping_enabled, because it annotates the
    # applicability verdict. When on, the post-L3 pass also stamps
    # payload['applicability'].{confidence_basis, confidence_label, confidence_reason}: a
    # deterministic rating of how legally solid the binds verdict is, independent of extraction
    # confidence (a binding duty is never rated 'low'). Annotation only: no verdict changes and
    # nothing is hidden.
    applicability_confidence_enabled: bool = False

    # SME role vocabulary for entity scoping. The base role vocabulary covers financial
    # institutions only, so an SME (legal_form='sro', for example) resolves to primary='unknown',
    # the fail-safe keeps every finding, and entity scoping can never demote anything for SMEs.
    # When on (requires entity_scoping_enabled), a project linked to a business_profile resolves its
    # role from the lawyer-reviewable SME table (legal form, the five gates and NACE) instead of the
    # finance-only project_entity_role. SME roles are disjoint from every financial AddresseeClass,
    # so they can only demote a financial obligation to 'conditional' (never out_of_scope) and
    # binding recall stays 1.0 by construction; an SME-addressed obligation resolves to 'yes'.
    # Before enabling: re-run the 79-item entity-scoping gold set, and have an advocate review the
    # SME table (sme_roles.py) and the fact-to-role mapping.
    sme_roles_enabled: bool = False

    # Citation precision in the digest. 'first_only' (default) shows only the primary cited
    # provision. 'all_direct' and 'all_with_hierarchy' also fill ObligationItem.all_cited_refs with
    # the full grounded citation set, without changing the primary citation or the case-law
    # grounding guard.
    digest_citation_strategy: Literal["first_only", "all_direct", "all_with_hierarchy"] = (
        "first_only"
    )

    # Multi-limb and duplicate annotation. When on, the digest annotates each obligation with limbs
    # (the split sub-duties), is_duplicate (a hint that render may collapse it) and n_alternatives
    # (mutually exclusive siblings, such as the 125k/300k capital pair). It only groups and flags;
    # it never drops a finding or a limb. Suppressing duplicate gaps is a separate switch below.
    multi_limb_enabled: bool = False

    # Temporal freshness. When on, the digest annotates each obligation with a freshness verdict
    # (current, superseded, future_effective, annulled or unknown, with reason, stale_since and so
    # on) computed from the cited provision's valid_from, valid_to and temporal_status against the
    # analysis as-of date. It flags stale law but never hides it; excluding stale gaps is a
    # separate switch below.
    temporal_freshness_enabled: bool = False

    # Gap suppression for the three annotations above, built as tag-not-drop so that a finding can
    # never disappear silently: L4 never filters a gap, it only stamps payload['suppressed_by'] with
    # a reason, and render moves a stamped gap to the visible Digest.context_gaps bucket instead of
    # deleting it.
    #   - entity_scoping_gap_suppression_enabled: demote a gap whose obligation is out_of_scope.
    #   - multi_limb_gap_dedup_enabled: demote a gap that duplicates an earlier one (same key).
    #   - temporal_gap_suppression_enabled: demote a gap whose provision is superseded or annulled.
    # Enabling any of them is a separate, lawyer-approved decision, because it sets aside a finding
    # the SME may rely on.
    entity_scoping_gap_suppression_enabled: bool = False
    multi_limb_gap_dedup_enabled: bool = False
    temporal_gap_suppression_enabled: bool = False

    # Competent-authority resolver ("kam podat / který úřad": where to file, which office). When
    # off, the answer-slot hook authority_resolver.maybe_resolve_authorities is a guaranteed no-op.
    # When on (and the operator has loaded the draft registry seed, topic map and gazetteer), the
    # practical "where/authority" slot maps an obligation or topic plus the asker's municipality to
    # the concrete competent office(s) through the pure resolver. The resolver never writes the
    # database and never fetches; loading the live directory (authority_registry.upsert_seed) is a
    # separate operator step. Before enabling: an advocate must review the draft authority seed and
    # the topic-to-authority competence map (both in tests/fixtures/authority/*.sample.json, marked
    # DRAFT), and the real directory fetch must exist.
    authority_registry_enabled: bool = False

    # Local seed and map files the authority resolver reads when authority_registry_enabled is on.
    # All default to None: if the flag is on without them, the slot builds an empty index and
    # resolves nothing rather than guessing an office. They are local file paths only; the
    # resolver never fetches and never writes the database.
    #   * authority_seed_path: the draft competent-authority registry JSON (parse_seed shape).
    #   * authority_topic_map_path: the topic-to-authority-type map JSON (parse_topic_map shape).
    #   * authority_gazetteer_path: the municipality to okres/ORP/kraj gazetteer (list of rows).
    #
    # A measurement run that sets AUTHORITY_REGISTRY_ENABLED=1 must also set these (names are
    # case-insensitive), otherwise the index is empty and the slot resolves nothing. Point them at
    # the committed draft fixtures, the same files regsuper.ingest.seed_authorities loads into the
    # database, so the database seed and the resolver read one source:
    #   AUTHORITY_SEED_PATH=tests/fixtures/authority/authority_registry.sample.json
    #   AUTHORITY_TOPIC_MAP_PATH=tests/fixtures/authority/topic_authority.sample.json
    #   AUTHORITY_GAZETTEER_PATH=data/geo/municipality_gazetteer.json  # full 6,258-row gazetteer
    # An advocate must validate the draft seed and topic map before this drives customer output.
    authority_seed_path: str | None = None
    authority_topic_map_path: str | None = None
    authority_gazetteer_path: str | None = None

    # Deployment environment. The app refuses to boot when require_auth is False in staging or
    # prod, and outside local the /internal/test/* routes require the control token.
    environment: Literal["local", "staging", "prod"] = "local"

    # Treat empty strings as unset for these optional fields, whether they come from the OS
    # environment or from an env file (see the cleanup loop at the top of this module).
    @field_validator(
        "openai_api_key",
        "anthropic_api_key",
        "cohere_api_key",
        "s3_endpoint_url",
        "s3_access_key",
        "s3_secret_key",
        "pythia_url",
        "control_panel_token",
        "pythia_database_url",
        "internal_secret",
        "conduit_oauth_issuer_url",
        "conduit_oauth_audience",
        "conduit_resource_uri",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, creating the log and data directories on first use."""
    s = Settings()
    s.log_dir.mkdir(parents=True, exist_ok=True)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


# ── Boot-time environment manifest ──────────────────────────────────────────────────────────────
# When AQ_ENV_MANIFEST_DIR is set, write_env_manifest dumps the fully resolved settings (secrets
# redacted) as JSON at startup, so the exact configuration a run booted with is recorded next to
# its artifacts rather than only in the runner. Unset by default, in which case nothing is written.
_SECRET_KEY_MARKERS = ("api_key", "password", "secret", "token")
_URL_KEY_MARKERS = ("url", "dsn")


def _redact_settings(dump: dict) -> dict:
    """Return a copy of a settings dump with secrets removed.

    Secret-looking keys are fully redacted; URL and DSN keys keep host and database but lose any
    ``user:pass@`` credentials. Pure; never raises on odd values.
    """
    import re

    out: dict = {}
    for k, v in dump.items():
        kl = k.lower()
        if any(m in kl for m in _SECRET_KEY_MARKERS):
            out[k] = "***REDACTED***" if v is not None else None
        elif any(m in kl for m in _URL_KEY_MARKERS) and isinstance(v, str) and "@" in v:
            out[k] = re.sub(r"//[^/@]*@", "//***REDACTED***@", v)
        else:
            out[k] = v
    return out


def write_env_manifest(*, component: str = "app") -> str | None:
    """Write the resolved settings, secrets redacted, to ``$AQ_ENV_MANIFEST_DIR`` at boot.

    Returns the written path, or None when the variable is unset or empty. Fail-soft: any error is
    swallowed, because a manifest write must never break startup.
    """
    import json

    target = os.environ.get("AQ_ENV_MANIFEST_DIR")
    if not target or not target.strip():
        return None
    try:
        d = Path(target.strip())
        d.mkdir(parents=True, exist_ok=True)
        payload = _redact_settings(get_settings().model_dump())
        path = d / f"regsuper-{component}-env-manifest.json"
        path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True))
        return str(path)
    except Exception:  # a manifest write must never break boot
        return None
