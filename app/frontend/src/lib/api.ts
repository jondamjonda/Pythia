// Tiny fetch wrapper for the Regulatory Super App backend (FastAPI on :8002).
// Day-14: every page hits these helpers; nothing in the UI talks to the
// backend without going through here.

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8002";

// ---------------------------------------------------------------------------
// Domain types — mirror the Pydantic shapes the backend returns. Kept slim
// (only the fields the UI consumes) so a backend change doesn't silently
// break the build.
// ---------------------------------------------------------------------------
export interface Project {
  id: string;
  name: string;
  sector: string;
  jurisdiction_scope: string[];
  entity_type: string | null;
  sector_tags: string[];
  activity_tags: string[];
  metadata: Record<string, unknown>;
}

export interface ProjectIntakePayload {
  name: string;
  sector: string;
  jurisdiction_scope: string[];
  entity_type?: string;
  declared_intent?: string;
  declared_risks?: string[];
  risk_tolerance?: string;
  analysis_depth?: string;
}

export interface Finding {
  id: string;
  finding_type: "applicable" | "obligation" | "gap" | string;
  statement: string;
  severity: string;
  cited_provisions: string[];
  cited_documents: string[];
  confidence_overall: number;
  analysis_layer: string;
  generated_by: string;
  review_state: string;
  payload: Record<string, unknown>;
}

export interface FindingsResponse {
  project_id: string;
  n_findings: number;
  findings: Finding[];
}

export interface AnalyzeRequest {
  top_k?: number;
  limit?: number | null;
  max_concurrency?: number;
  force?: boolean;
  layers?: Array<"L2" | "L3" | "L4">;
}

export interface AnalyzeSection {
  n_evaluated?: number;
  n_skipped_cached?: number;
  n_applicable?: number;
  n_ambiguous?: number;
  n_not_applicable?: number;
  n_errors?: number;
  n_findings_persisted?: number;
  // L3 fields:
  n_obligations_extracted?: number;
  n_obligations_persisted?: number;
  // L4 fields:
  n_obligations_scanned?: number;
  n_gaps_persisted?: number;
  n_client_documents?: number;
  latency_ms?: number;
}

export interface AnalyzeResponse {
  project_id: string;
  layers: string[];
  l2?: AnalyzeSection;
  l3?: AnalyzeSection;
  l4?: AnalyzeSection;
  total_latency_ms: number;
}

export interface HierarchyNode {
  id: string;
  label: string | null;
  hierarchy_path: string;
}

export interface ProvisionView {
  id: string;
  label: string | null;
  text: string;
  text_format: string;
  hierarchy_path: string;
  legal_hierarchy: string;
  jurisdiction: string;
  language: string;
  source_document_id: string;
  source_external_id: string | null;
  source_title: string;
  source_publisher: string;
  parent_id: string | null;
  ancestors: HierarchyNode[];
  cross_references: HierarchyNode[];
  // B2b-12 — trust/temporal badges; null until the corpus backfill (0/7895 today) → render nothing.
  bindingness_tier: string | null;
  temporal_status: string | null;
}

export interface RetrieveRequest {
  query: string;
  jurisdiction?: string[];
  sector?: string;
  top_n_rerank?: number;
  expand_top_n?: number;
}

export interface RetrieveResultItem {
  rank: number;
  provision_id: string;
  label: string | null;
  external_id: string | null;
  title: string | null;
  legal_hierarchy: string | null;
  jurisdiction: string | null;
  rrf_score: number;
  rerank_score: number | null;
}

export interface RetrieveExpandedNode {
  id: string;
  label: string | null;
  hierarchy_path?: string;
}
export interface RetrieveExpandedDefinition {
  term: string;
  definition_provision_id: string;
  extracted_by: string;
  confidence: number;
}
export interface RetrieveExpanded {
  hit: { id: string; label: string | null; hierarchy_path: string; text: string };
  ancestors: RetrieveExpandedNode[];
  cross_references: RetrieveExpandedNode[];
  definitions: RetrieveExpandedDefinition[];
}

export interface RetrieveResponse {
  query: string;
  refused: boolean;
  // `flagged` = answer shown but low-confidence (rerank skipped → no
  // calibrated score). Render a caution banner when flagged && !refused.
  flagged: boolean;
  reason: string | null;
  top_rerank_score: number | null;
  // Which refusal threshold was applied and its provenance
  // ("calibrated" = published fit, "fallback" = the 0.6 placeholder).
  refusal_threshold: number;
  refusal_source: string;
  results: RetrieveResultItem[];
  expanded: RetrieveExpanded[];
  diagnostics: {
    latency_ms: number;
    rerank_skipped: boolean;
  } & Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Answer surface (Phase C / M6+M8) — the consumer "ask a legal question" wire shape.
// Mirrors the app-owned AnswerResponse from src/regsuper/api/answer.py (a STABLE
// contract the FE depends on, NOT the engine's internal PythiaAnswer). EVERY field
// past `render_mode` is optional/defensive: the API may legitimately omit a field
// (NULL-when-NULL) and the UI must render nothing rather than fabricate a value.
// ---------------------------------------------------------------------------

// The six honest render frames. `full`/`hedged` are answers; the other four are
// honest non-answers (never a fabricated answer). Unknown/absent → treat as a
// refuse-style honest frame in the UI, never as a confident answer.
export type RenderMode =
  | "full"
  | "hedged"
  | "closest"
  | "notlegal"
  | "falsepremise"
  | "refuse"
  // Track A / VL-A — the STAGE-1 clarify frame. NOT a refusal (refused=false) and
  // NOT a non-answer: the server needs ≤3 decision-relevant facts before answering.
  // The UI routes this to a slot-collection FORM (render `questions`, resubmit
  // `slots`), never to the refuse/degradation UI. See AnswerView / ClarifyForm.
  | "clarify";

// The three CZ answer sections. `kind` is the machine key; `heading` is the
// (server-supplied) display string. Judikatura is capability-flagged OFF in the
// UI regardless of what the server sends (case-law honesty invariant).
export type SectionKind =
  | "legislative_frame"
  | "case_law"
  | "assessment"
  | string;

// A clickable citation derived from the engine `citations_map`. `span` = the
// {start,end} character offsets into the section text (optional — the UI does not
// require it to render the chip). `corpus` is stamped "pythia" by M6 so the
// chip-resolver deep-links into the right source viewer.
export interface AnswerChip {
  provision_id: string;
  label?: string | null;
  corpus?: string | null;
  span?: { start: number; end: number } | null;
  section_kind?: SectionKind | null;
}

export interface AnswerSection {
  kind: SectionKind;
  heading?: string | null;
  // The drafted prose. MUST be rendered as escaped/inert text (never as HTML) —
  // see AnswerSection.tsx. A field absent → the section renders no prose.
  text?: string | null;
  chips?: AnswerChip[] | null;
}

// The MetaVerdict block (route/intent/coverage/premise + grounded fallbacks).
// All optional — populated only when the engine metagate is ON; absent under the
// default (shadow/off) config, in which case the UI shows none of it.
export interface AnswerMeta {
  route?: string | null;
  intent?: number | null;
  coverage?: number | null;
  premise?: number | null;
  // closest related provisions for the `closest` (not_in_corpus) frame — real chips.
  closest_provisions?: AnswerChip[] | null;
  // the false-premise correction text + its grounding citations.
  correction?: string | null;
  correcting_citations?: AnswerChip[] | null;
  [k: string]: unknown;
}

// T7 — the UNDERSTANDING PANEL artifact (how the engine understood the layman
// question). Present only when the server flag `answer_intake_panel_enabled` is ON
// AND the engine attached `audit_trace["intake"]`. Every field is optional/defensive:
// absent → the panel is not rendered and the essay view is byte-identical.
export interface IntakeFact {
  field: string;
  value: string;
  source?: string | null; // det | llm | profile
}

export interface IntakeDropped {
  field: string;
  reason?: string | null; // DRAFT machine reason (advocate-review-pending, G14)
}

// T7 — a flagged intake conflict, whitelisted to a field + its candidate values
// (no free-text passes through from the server).
export interface IntakeConflict {
  field: string;
  values?: string[] | null;
}

export interface IntakeBlock {
  sub_questions?: string[] | null;
  facts?: IntakeFact[] | null;
  conflicts?: IntakeConflict[] | null;
  sufficiency?: string | null;
  dropped?: IntakeDropped[] | null;
}

// T7 — one consented profile write-back suggestion (the asker's own whitelisted
// fact). Offered with a one-click "uložit do profilu" affordance; NEVER auto-written.
export interface ProfileSuggestion {
  field: string;
  value: string;
}

// W4 / T13 — one CONCRETE competent office resolved for the practical "kam se
// obrátit / where" slot. Mirrors the app-owned `AuthorityOut` (src/regsuper/api/
// answer.py): the WHITELISTED shape is `authority_type` (shared vocab tag), `name`
// (the DRAFT registry seed office name), `match_kind` ('national' | okres | orp |
// kraj), and `is_draft` (always true — the seed is advocate-review-pending, G14).
// `source_uri` + `fetched_at` are OPTIONAL provenance the server may whitelist onto
// the entry (the directory source URL + the fetch/as-of date); when present the FE
// renders the source link + a "stav k {fetched_at}" as-of note. Absent → the entry
// still renders its name/office WITHOUT a source line. This block is a NON-LEGAL
// directory aid — it NEVER becomes a citation chip and carries a clear non-legal
// label. Populated ONLY when the server flag `authority_registry_enabled` is ON and
// a known municipality + a mapped topic resolved an office; otherwise the list is
// empty (byte-identical). NEVER a guessed office.
export interface Authority {
  authority_type: string;
  name: string;
  match_kind: string;
  is_draft?: boolean | null;
  source_uri?: string | null;
  fetched_at?: string | null;
}

// Track A / VL-A — one STAGE-1 clarifying question on a `render_mode="clarify"`
// frame. Mirrors the app-owned `ClarifyQuestionOut` (src/regsuper/api/answer.py):
// `slot` is the key the FE echoes back in the follow-up `slots` map; `text` is the
// fixed DRAFT question copy (the LLM never writes it). The wire carries NO options —
// the FE renders a plain input (or a select over known slot values when it has them).
export interface ClarifyQuestion {
  slot: string;
  text: string;
}

// The stable app-owned answer wire contract. Past `render_mode` everything is
// optional so a partial/older response never crashes the view.
export interface AnswerResponse {
  render_mode: RenderMode;
  refused?: boolean | null;
  reason?: string | null;
  sections?: AnswerSection[] | null;
  chips?: AnswerChip[] | null;
  meta?: AnswerMeta | null;
  band?: string | null;
  // Honest framing strings the server supplies (lawyer-reviewed copy under G14).
  // The components render WHATEVER copy is supplied — never invent legal text.
  title?: string | null;
  body?: string | null;
  disclaimer?: string | null;
  hedge?: string | null;
  hedge_banner?: string | null;
  as_of_unknown_note?: string | null;
  // T7 — the UNDERSTANDING PANEL + its consented profile write-back suggestions.
  intake?: IntakeBlock | null;
  profile_suggestions?: ProfileSuggestion[] | null;
  // W4 / T13 — the concrete competent office(s) for the "kam se obrátit / where"
  // slot. A NON-LEGAL directory aid, rendered in a labeled block SEPARATE from the
  // citations. Empty/absent → no block is rendered (byte-identical).
  authorities?: Authority[] | null;
  // Track A / VL-A — the STAGE-1 clarify frame. `questions` (≤3) is populated ONLY on a
  // `render_mode="clarify"` frame (empty on every other frame); the FE renders them as a
  // slot-collection form and resubmits the SAME query with the collected `slots`. A clarify
  // is refused=false. `matter_type` records the deterministically-typed matter (transparency).
  // `assumptions` surfaces the slot names the engine HEDGED over when a follow-up still left
  // required slots unfilled (turn-2 metadata only — grounds no claim).
  questions?: ClarifyQuestion[] | null;
  matter_type?: string | null;
  assumptions?: string[] | null;
}

export interface AnswerOptions {
  // ISO date (YYYY-MM-DD) point-in-time query. A NULL valid_from is treated
  // in-force server-side; the temporal note ("datum účinnosti neuvedeno") rides
  // `as_of_unknown_note`, never a fabricated date.
  as_of?: string;
  project_id?: string;
  // Track A / VL-A — the STAGE-1 clarify FOLLOW-UP. Turn 1 carries no `slots`; the
  // resubmit (turn 2) carries the user's answers to the clarify `questions` keyed by
  // slot. Sent ONLY when non-empty (byte-identical single-shot body otherwise). The
  // server (MAX 1 round) never clarifies again once `slots` is present.
  slots?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------
async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export interface CorpusAct {
  ext: string;
  title: string;
  jurisdiction: string | null;
  hierarchy: string | null;
  provisions: number;
}
export interface CorpusOverview {
  documents: number;
  provisions: number;
  acts: CorpusAct[];
}

// ---------------------------------------------------------------------------
// Digest + profiles surface (Phase C / LEDGER-M9) — the SME profile-intake +
// plain-language compliance digest. Independent data path from the answer
// surface: these read the app's OWN finding/provision tables (the regsuper
// corpus), NOT pythia. Mirrors src/regsuper/api/{profiles,digest}.py.
//
// NULL-WHEN-NULL: every optional field (deadline, legal_source_url, …) is
// rendered as nothing when absent — the UI never fabricates a trust/binds badge
// or a date. Citation chips deep-link via CitationLink with NO ?corpus param
// (regsuper corpus = the app DB, the viewer's default read).
// ---------------------------------------------------------------------------

// The SME described once. `characteristics` are the five boolean compliance
// gates (processes_personal_data / handles_client_money / has_real_estate /
// cross_border / is_employer). All fields past `name` are optional.
export interface ProfilePayload {
  name: string;
  legal_form?: string;
  ico?: string;
  nace_code?: string;
  headcount_band?: string;
  primary_jurisdiction?: string;
  preferred_language?: string;
  // AQ-05 — validated RÚIAN/ČSÚ obec code (never free text). Set by the
  // municipality autocomplete from a picked GET /api/municipalities hit; the
  // server still 422s an unknown code on save.
  municipality_code?: string;
  characteristics?: Record<string, boolean>;
}

// AQ-05 — one hit from GET /api/municipalities?q= (mirrors MunicipalityMatch in
// src/regsuper/api/municipalities.py). `code` is the value the form stores.
export interface MunicipalityMatch {
  name: string;
  code: string;
  okres: string | null;
  orp: string | null;
  kraj: string | null;
}

export interface BusinessProfile {
  id: string;
  name: string;
  legal_form: string | null;
  ico: string | null;
  nace_code: string | null;
  headcount_band: string | null;
  primary_jurisdiction: string | null;
  preferred_language: string;
  sector_tags: string[];
  activity_tags: string[];
}

// One plain-language obligation. `binds` is the WF-1 entity/role verdict: 'yes'
// (binds you), 'conditional' (binds only if binds_reason holds), 'out_of_scope'
// (does NOT bind — the server demotes these into ObligationRegisterResponse
// .does_not_bind, never dropped). `deadline` is NULL-when-NULL.
export interface ObligationItem {
  modal_label: string;
  actor_label: string;
  deadline?: string | null;
  legal_source: string;
  legal_source_url?: string | null;
  cited_provision_id: string;
  confidence: number;
  review_state: string;
  binds?: "yes" | "conditional" | "out_of_scope";
  binds_reason?: string;
  confidence_label?: string;
  confidence_reason?: string;
}

export interface ChecklistItem {
  obligation: ObligationItem;
  // CZ status literals: splněno | nesplněno | neověřeno | nejasné.
  status: string;
}

export interface GapItem {
  statement: string;
  // CZ severity literals: kritické | důležité | doporučené.
  severity_label: string;
  legal_source: string;
  cited_provision_id: string;
  // WF-1 Inc5 / WF-4 / WF-6: demotion reason(s); present on context_gaps.
  suppressed_by?: string[];
}

export interface ActionItem {
  statement: string;
  // CZ priority literals: okamžitě | do 30 dnů | do 90 dnů | průběžně.
  priority: string;
  legal_source: string;
}

// Every digest response carries these — render the disclaimer + an
// unreviewed-citations banner verbatim (escaped text), never invented copy.
interface DigestBase {
  profile_id: string;
  project_id: string;
  lang: string;
  disclaimer?: string;
  has_unreviewed_citations?: boolean;
  tier_answer_mode?: string | null;
}

export interface ObligationRegisterResponse extends DigestBase {
  obligations: ObligationItem[];
  // WF-1: out_of_scope obligations DEMOTED here — surfaced for transparency,
  // shown in their own visible section, NEVER dropped.
  does_not_bind: ObligationItem[];
}

export interface ComplianceChecklistResponse extends DigestBase {
  items: ChecklistItem[];
}

export interface GapReportResponse extends DigestBase {
  gaps: GapItem[];
  // demoted-not-dropped gaps (out_of_scope / duplicate / stale).
  context_gaps: GapItem[];
}

export interface ActionListResponse extends DigestBase {
  actions: ActionItem[];
}

export interface DigestSummaryResponse extends DigestBase {
  obligations: ObligationItem[];
  checklist: ChecklistItem[];
  gaps: GapItem[];
  actions: ActionItem[];
  does_not_bind: ObligationItem[];
  context_gaps: GapItem[];
}

// Mirrors ProfileAnalyzeRequest — all fields default server-side, so the FE can
// POST an empty body to run the standard L2+L3+L4 cascade.
export interface ProfileAnalyzeBody {
  top_k?: number;
  limit?: number | null;
  max_concurrency?: number;
  force?: boolean;
  layers?: Array<"L2" | "L3" | "L4">;
}

// Shared query options for the five digest views.
export interface DigestQuery {
  lang?: string;
  project_id?: string;
  limit?: number;
  offset?: number;
}

function digestQuery(opts: DigestQuery): string {
  const q = new URLSearchParams();
  if (opts.lang) q.set("lang", opts.lang);
  if (opts.project_id) q.set("project_id", opts.project_id);
  if (opts.limit != null) q.set("limit", String(opts.limit));
  if (opts.offset != null) q.set("offset", String(opts.offset));
  const qs = q.toString();
  return qs ? `?${qs}` : "";
}

// ---------------------------------------------------------------------------
// Public API surface
// ---------------------------------------------------------------------------
export const api = {
  createProject: (body: ProjectIntakePayload): Promise<Project> =>
    http<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),

  getProject: (id: string): Promise<Project> =>
    http<Project>(`/api/projects/${id}`),

  analyzeProject: (id: string, body: AnalyzeRequest = {}): Promise<AnalyzeResponse> =>
    http<AnalyzeResponse>(`/api/projects/${id}/analyze`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getFindings: (id: string, params: { layer?: string; limit?: number } = {}): Promise<FindingsResponse> => {
    const q = new URLSearchParams();
    if (params.layer) q.set("layer", params.layer);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return http<FindingsResponse>(`/api/projects/${id}/findings${qs ? `?${qs}` : ""}`);
  },

  // `corpus` selects which DB to read: undefined = app DB (default); "pythia" reads the
  // read-only graph corpus so the act-graph diagram's deep-links resolve for all 628 acts.
  getProvision: (id: string, corpus?: string): Promise<ProvisionView> =>
    http<ProvisionView>(`/api/provisions/${id}${corpus ? `?corpus=${encodeURIComponent(corpus)}` : ""}`),

  retrieve: (body: RetrieveRequest): Promise<RetrieveResponse> =>
    http<RetrieveResponse>("/internal/retrieve", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Phase-C consumer answer surface (M8 → M6 `POST /api/answer`). SINGLE-SHOT /
  // stateless by contract: we send exactly one self-contained `query` (+ optional
  // as_of / project_id) and NEVER concatenate conversation history. The server gate
  // is `answer_endpoint_enabled` (404 when off → the UI renders its off state).
  answer: (query: string, opts: AnswerOptions = {}): Promise<AnswerResponse> =>
    http<AnswerResponse>("/api/answer", {
      method: "POST",
      body: JSON.stringify({
        query,
        ...(opts.as_of ? { as_of: opts.as_of } : {}),
        ...(opts.project_id ? { project_id: opts.project_id } : {}),
        // Track A / VL-A — the clarify FOLLOW-UP. Sent only when non-empty so a
        // first-turn ask stays a byte-identical single-shot body.
        ...(opts.slots && Object.keys(opts.slots).length > 0
          ? { slots: opts.slots }
          : {}),
      }),
    }),

  // -------------------------------------------------------------------------
  // Digest + profiles (LEDGER-M9). Server gates are `profiles_enabled` /
  // `digest_enabled` — both 404 when off, which the consumer pages surface as
  // an honest off-state (never an error toast). Reads the app DB (regsuper).
  // -------------------------------------------------------------------------
  createProfile: (body: ProfilePayload): Promise<BusinessProfile> =>
    http<BusinessProfile>("/api/profiles", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getProfile: (id: string): Promise<BusinessProfile> =>
    http<BusinessProfile>(`/api/profiles/${id}`),

  // AQ-05 — read-only municipality name search for the profile-form autocomplete.
  // Case/diacritic-insensitive prefix on the name; returns up to ~10 hits. Read-only.
  searchMunicipalities: (q: string): Promise<MunicipalityMatch[]> =>
    http<MunicipalityMatch[]>(`/api/municipalities?q=${encodeURIComponent(q)}`),

  // T7 — partial update, reusing the existing PATCH /api/profiles/{id} (ProfilePatch).
  // Used by the UNDERSTANDING PANEL's consented "uložit do profilu" write-back: the
  // asker's own whitelisted facts are folded into the standing business profile ONLY
  // on explicit user action (never auto-written). Server-gated by `profiles_enabled`
  // (404 when off), which the caller surfaces as an honest off-state, never an error.
  patchProfile: (
    id: string,
    body: Partial<ProfilePayload> & {
      characteristics?: Record<string, boolean>;
      // VL-A-11 / A3.5 — opts the write-back into the server-side only-fill-EMPTY
      // guard: a suggestion may fill a field ONLY when it is currently empty and
      // NEVER overwrites an existing value. Enforced server-side (defence-in-depth).
      suggested_only?: boolean;
    },
  ): Promise<BusinessProfile> =>
    http<BusinessProfile>(`/api/profiles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  // SYNCHRONOUS by contract (no async worker today) — returns the same
  // AnalyzeResponse as POST /api/projects/{id}/analyze once the cascade runs.
  analyzeProfile: (id: string, body: ProfileAnalyzeBody = {}): Promise<AnalyzeResponse> =>
    http<AnalyzeResponse>(`/api/profiles/${id}/analyze`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // The five read-only digest views (latest run for the profile, or an explicit
  // ?project_id=). Each carries disclaimer + has_unreviewed_citations.
  getDigestObligations: (
    profileId: string,
    opts: DigestQuery = {},
  ): Promise<ObligationRegisterResponse> =>
    http<ObligationRegisterResponse>(
      `/api/digest/${profileId}/obligations${digestQuery(opts)}`,
    ),

  getDigestChecklist: (
    profileId: string,
    opts: DigestQuery = {},
  ): Promise<ComplianceChecklistResponse> =>
    http<ComplianceChecklistResponse>(
      `/api/digest/${profileId}/checklist${digestQuery(opts)}`,
    ),

  getDigestGaps: (
    profileId: string,
    opts: DigestQuery = {},
  ): Promise<GapReportResponse> =>
    http<GapReportResponse>(`/api/digest/${profileId}/gap-report${digestQuery(opts)}`),

  getDigestActions: (
    profileId: string,
    opts: DigestQuery = {},
  ): Promise<ActionListResponse> =>
    http<ActionListResponse>(`/api/digest/${profileId}/actions${digestQuery(opts)}`),

  getDigestSummary: (
    profileId: string,
    opts: DigestQuery = {},
  ): Promise<DigestSummaryResponse> =>
    http<DigestSummaryResponse>(`/api/digest/${profileId}/summary${digestQuery(opts)}`),

  corpusOverview: (): Promise<CorpusOverview> =>
    http<CorpusOverview>("/internal/corpus-overview"),

  // SSE event-stream URL — the consumer attaches an EventSource directly
  // because fetch() can't stream SSE in a browser-friendly way.
  eventStreamUrl: (projectId: string): string =>
    `${API_URL}/api/projects/${projectId}/events`,
};
