"""Engine settings, read from PYTHIA_* environment variables (and an optional .env file).

Most switches here are feature flags that default to off; the comment above each one says what
turning it on changes. The engine reads the legal corpus and writes graph edges in its own Postgres
database.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class PythiaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PYTHIA_", env_file=(".env",), extra="ignore")

    # The control-plane `data/` directory shared with the Polarius app for cross-process signals: the
    # app writes `KILL_SWITCH` there (checked by the engine between batches) and the engine writes
    # `gpu_mps.json` (read by the app's MPS gate and System panel). The app's virtualenv has no torch,
    # so only a live torch process in the engine (an embedding or GNN run) can write the MPS
    # heartbeat. The default points at a Polarius checkout next to this repository; override with
    # PYTHIA_DATA_DIR.
    data_dir: Path = Path("../../Polarius/data")

    # Pythia's own corpus database `pythia`. It lives in the same Postgres instance as the app's
    # `regsuper` database (it was cloned from it once); the engine owns the legal corpus and grows it
    # independently. Override with PYTHIA_DATABASE_URL.
    database_url: str = "postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia"

    embedding_dim: int = 1024  # provision.embedding_bge (BAAI/bge-m3); the mpnet fallback is 768
    memory_dim: int = 64  # pythia_edge.memory_vec (ReMindRAG-style edge memory)
    gnn_hidden_dim: int = 512
    max_hops_default: int = 3  # "four corners" expansion depth
    decay_base: float = 0.6  # per-hop score decay in the CTE expansion

    # Clause-level read path (default off). When on (PYTHIA_CLAUSE_READ_ENABLED=1), seed.dense_seed
    # adds an ANN leg over the `provision_clause` index and fuses the parent provision ids of clause
    # hits into the seed set; the graph, the GNN and references stay anchored at section level. Enable
    # it only after the clause index has been built and embedded.
    clause_read_enabled: bool = False

    # Cross-encoder reranker (bge-reranker-v2-m3, about 2-2.5 GB resident) that rescores the seed
    # shortlist on every answer. On by default for better ranking. A memory-constrained host can set
    # PYTHIA_SEED_RERANK_ENABLED=0 to skip this second large transformer; the bi-encoder cosine seed
    # still returns a valid ranked shortlist, only with lower ranking quality.
    seed_rerank_enabled: bool = True

    # Change-tracking capabilities, all default off and inert until enabled.
    discovery_enabled: bool = False  # publication-feed watcher writes discovery_queue (no ingestion)
    gnn_staleness_check_enabled: bool = False  # GNNRetriever.load warns on graph-fingerprint drift
    graph_changed_monitor_enabled: bool = False  # graph-changed consumer emits gnn-stale (no retrain)
    version_diff_enabled: bool = False  # micro/macro version diff, impact services, amendment_impact rows

    # Temporal substrate (default off). It maintains two provision columns: embedded_text_hash (the
    # text hash a vector was computed from, so stale vectors can be found) and pit_status (each
    # provision's own point-in-time status). When off, the embedder, refresh._reindex and
    # scheduler.run_temporal neither create nor write these columns (run_temporal only recomputes the
    # document-level status), so a corpus that never activated the substrate is left untouched. When
    # on (PYTHIA_TEMPORAL_SUBSTRATE_ENABLED=1), new embeddings stamp the hash, the scheduler
    # recomputes pit_status, amendments emit events and the as-of text overlay activates.
    #
    # One-time activation on an existing corpus (take a database snapshot first; this is a cheap
    # stamp backfill, not a re-embed):
    #   1. ADD COLUMN IF NOT EXISTS embedded_text_hash / pit_status, plus the two partial indexes.
    #   2. Stamp the existing vectors:
    #        UPDATE provision SET embedded_text_hash = text_hash WHERE embedding_bge IS NOT NULL
    #      Do not use --reembed-stale for this initial backfill: with embedded_text_hash still NULL it
    #      treats every embedded row as stale and re-embeds the whole corpus.
    #   3. Run recompute_provision_temporal_status(apply=True) to fill pit_status.
    #   4. Check that the d1 / n5 / n6 integrity checks pass.
    # To keep it maintained afterwards, set the flag in the serving engine's launch environment, not
    # in `.env` (the test suite loads `.env`, which would break the default-off tests). The added
    # columns and indexes are harmless if the flag is later unset. reembed_stale (only_missing=False
    # over the changed set) is for reconciling a real in-place amendment after the initial stamp.
    # reembed_stale, recompute_provision_temporal_status and stale_vector_ids stay directly callable;
    # the flag gates only the automatic cron and refresh entry points.
    temporal_substrate_enabled: bool = False

    # Temporal consumer (default off, display only). Independent of the substrate flag: the substrate
    # maintains the columns, this flag surfaces them. When on (PYTHIA_TEMPORAL_CONSUMER_ENABLED=1),
    # fetch_provisions and candidates also select p.pit_status and a computed stale_vector (an
    # embedding exists and embedded_text_hash <> text_hash), which fill the CitedProvision display
    # facets pit_status / vector_stale. An information_schema guard falls back to the plain select
    # with None facets on a corpus without the substrate columns. It does not affect trust
    # resolution, the tier map, the candidate set, ranking or refusal.
    temporal_consumer_enabled: bool = False

    # Temporal down-weighting (default off, changes behaviour). Stops surfacing law that has been
    # superseded at provision level as a current must-mention rule. When on
    # (PYTHIA_TEMPORAL_DOWNWEIGHT_ENABLED=1), every resolve_trust caller passes downweight=True and
    # the resolver refines the document-level temporal_status with the provision's pit_status. The
    # refinement only ever restricts: a provision whose head version is SUPERSEDED is no longer
    # must-surfaced as current even if its act is in force, while a superseded or annulled act is
    # never made active again by pit_status. The provision is still retrieved (candidates and ranking
    # are unchanged); only the must_surface / temporal_status trust signal changes.
    # ports._provision_cols also selects pit_status when this flag is on and the substrate columns
    # exist, so the refinement has its input even with the consumer flag off.
    temporal_downweight_enabled: bool = False

    # Temporal prose guard (default off, read-only). Down-weighting keeps a superseded rule out of the
    # structured obligation register, but the provision is still retrieved as context and the
    # free-text answer would otherwise carry no temporal signal, so the model could present repealed,
    # superseded or future law as current. When on (PYTHIA_TEMPORAL_PROSE_GUARD_ENABLED=1), each
    # provision line in the prompt context carries a caveat ([WARN SUPERSEDED ...] /
    # [WARN NOT YET IN FORCE ...]) derived from pit_status and the document-level temporal_status, and
    # the generation prompt tells the model never to present a marked provision as current law. It
    # only adds caveats to LLM-facing text: it never drops, reorders or refuses a citation and never
    # changes retrieval, cited provisions, the obligation register, sections, citations_map or
    # confidence. ports._provision_cols also selects pit_status when this flag is on.
    temporal_prose_guard_enabled: bool = False

    # As-of text rendering (default off, read-only). With the substrate on, seeds and graph hits carry
    # asof_text: the provision_version text whose [valid_from, valid_to) window contains a past as_of.
    # When on (PYTHIA_TEMPORAL_ASOF_RENDER_ENABLED=1), that version text is served instead of the
    # current head text in two places: CitedProvision.text_md on /v1/retrieve (still truncated to 240
    # characters), and the prose context and cited text on /v1/answer, which reasoning/answer.py reads
    # through NexusPort.asof_text when a strictly past as_of is supplied. Without a version match, or
    # with a missing, current or future as_of, the head text is used, so on a corpus where every
    # provision has a single version the flag has no visible effect. It never changes which
    # provisions are fetched or cited, their order, trust facets, the obligation register, confidence,
    # refusal or ranking.
    temporal_asof_render_enabled: bool = False

    # Current-only filter (default off, changes the candidate set). When on
    # (PYTHIA_TEMPORAL_CURRENT_ONLY_ENABLED=1) and provision.pit_status exists, retrieval drops
    # provisions known not to be current, using the fail-open predicate
    # `(p.pit_status IS NULL OR p.pit_status = 'IN_FORCE')`. SUPERSEDED and NOT_YET_IN_FORCE are
    # excluded; IN_FORCE and NULL are kept, because NULL means the provision has no version lineage
    # (status unknown) and dropping it would silently lose real current law. For the same reason the
    # partial index ix_provision_in_force (pit_status = 'IN_FORCE') is deliberately not used. The
    # document-level valid_from / valid_to date mask remains the primary in-force gate.
    # Every retrieval method is covered, all through the shared seed.current_only_clause predicate:
    #   - NexusPort.candidates (the /v1/candidates "in-force law matching these facets" endpoint);
    #   - seed retrieval (sparse, dense, clause) and the four_corners CTE;
    #   - the PPR edge view, on both endpoints (the fingerprint SQL wraps the same select, so the
    #     cached graph is rebuilt when the flag changes);
    #   - the GNN walk, as a post-retrieval drop, because the in-memory walk has no SQL mask (so gnn
    #     may return fewer than k hits).
    # A corpus without the pit_status column falls back to the unfiltered query.
    temporal_current_only_enabled: bool = False

    # Relevance fusion (default off). When on (PYTHIA_FUSION_ENABLED=1), engine.retrieve() re-ranks
    # the merged hits by combining the graph score with semantic cosine via RRF, plus a protected
    # floor for rule provisions; when off it keeps the structural sort. This global flag also governs
    # the app's /v1/retrieve lane, so it is meant to be enabled per domain, after calibration.
    fusion_enabled: bool = False

    # Fusion for the answer path only (default off), without touching the global `fusion_enabled`.
    # When on, answer() builds each per-leg PythiaQuery with fusion=True, so only the answer path
    # fuses while the /v1/retrieve lane keeps following `fusion_enabled`. When off, q.fusion stays
    # None and engine.retrieve falls back to `fusion_enabled`. Uses the default FusionParams weights;
    # calibrating them needs a certified gold set.
    answer_path_fusion_enabled: bool = False

    # Wider LLM context windows (default off, read-only). reasoning/answer.py::_fetch truncates each
    # provision to 700 characters for the generation context and the faithfulness check, yet in 52%
    # of provisions with an explicit "do N dnů" (within N days) deadline the deadline appears after
    # character 700, so neither the model nor the fact checker can see it. (The cookie rule in
    # 127/2005 Sb., §89 sits around character 1278 with no fact token, so fact-only windows miss it
    # too.) When on (PYTHIA_CTX_WINDOW_ENABLED=1), _fetch adds a separate meta['ctx_txt']: the first
    # 700 characters plus ±250-character windows around (a) practical_facts.extract_practical_facts
    # hits and (b) query and sub-query term matches, merged, deduplicated and capped at about 1600
    # characters per provision. _build_ctx and the faithfulness check read ctx_txt when the flag is
    # on. The served text (meta['txt'], which becomes CitedProvision.text_md and the citation chips)
    # stays the 700-character slice, so the app and frontend see no change. The raw text comes from
    # the row already fetched (no extra database call) and is capped at about 20k characters before
    # windowing. When off, ctx_txt equals txt. Section headings, prompt shape and the set and order of
    # cited ids are unchanged either way.
    ctx_window_enabled: bool = False

    # Clause-snapped context windows (default off, read-only; acts only where ctx_window_enabled
    # widened a span). A ±250-character window can straddle clause boundaries: a deadline in odst. (2)
    # gets quoted together with the tail of odst. (1) and the head of odst. (3), so the model sees
    # three half-clauses instead of the one clause that carries the rule. When on
    # (PYTHIA_CTX_CLAUSE_SNAP_ENABLED=1), each widened window is snapped to the smallest enclosing
    # clause, read from the existing `provision_clause` index (the table clause_read_enabled uses; no
    # re-chunking, no writes), under the same ~1600 cap, and each kept span carries a `clause_label`
    # such as "(2)" into the evidence bundle. A missing or empty provision_clause table makes
    # NexusPort.clauses_for return {}, which means no snapping. When off, clauses_for is never
    # called. The served text and the set and order of cited ids are unchanged. Independent of
    # clause_read_enabled, which adds clause hits to the seed set rather than re-cutting context text.
    ctx_clause_snap_enabled: bool = False

    # Structured seven-slot action-plan answer (default off). The default answer is a legal essay with
    # three headings (Legislativní rámec / Judikatura / Hodnocení) that reads like a memo: a small
    # business cannot easily see what applies to it, the concrete deadlines and steps, or where to
    # turn. When on (PYTHIA_STRUCTURED_ANSWER_ENABLED=1), _GEN_PROMPT_STRUCTURED produces a fixed
    # seven-slot Czech action plan: Vaše situace / Co se na vás vztahuje / Konkrétní povinnosti a lhůty
    # / Postup krok za krokem / Kam se obrátit / Sankce a rizika / Co korpus nepokrývá. It draws on
    # the extracted practical facts (verbatim deadline, authority, penalty and form excerpts) and the
    # user's profile, both fenced in the prompt. reasoning.planner.plan_slots routes each retrieved
    # provision to a slot before generation (deterministic, by role, section_kind and facts; it adds,
    # never gates), and the plan is attached to audit_trace as `slot_plan`. slot_verify, a pure
    # per-slot grounding check, demotes an ungrounded slot to an honest gap instead of letting it be
    # fabricated. PythiaAnswer.practical carries the routed facts, gaps and profile_used. When off,
    # answer() uses the essay prompt and three-heading splitter, and PythiaAnswer.practical is None.
    # The final citation check (verify.py) is the same either way. The user-facing Czech copy is a
    # draft pending review by an advocate, and the prompt keeps the answer descriptive: it informs
    # the user and does not act on their behalf.
    structured_answer_enabled: bool = False
    # Optional cheap-LLM refinement of the slot plan (default off; only meaningful together with
    # structured_answer_enabled). When off, slot routing is fully deterministic, so the plan is
    # auditable and free, which is why it is the default. When on, reasoning.planner.plan_slots may
    # make one cheap JSON call that moves a provision to a better slot. The reply is validated (ids
    # that were not retrieved are dropped, omitted provisions are re-added, any error falls back to
    # the deterministic plan), so it can never drop or invent a provision.
    structured_plan_llm_enabled: bool = False
    # Typed draft plan (default off). After the drafted answer is split into `sections`, the
    # deterministic planner (reasoning/draft_plan.py) lifts them into a contract.DraftPlan: the ordered
    # slots (kind and draft-relative range) with per-slot source chips and their provision_ids, so
    # claim verification can consume a typed plan instead of re-deriving its partition from the
    # rendered sections. The view is lossless: sections_from_plan(plan_from_sections(secs))
    # reproduces the sections exactly (an equivalence test checks this). When on
    # (PYTHIA_DRAFT_PLANNER_ENABLED=1), the plan is built purely from the sections (no LLM, database
    # or clock) and attached as audit_trace["draft_plan"]; the served answer is unchanged. Claim
    # atomization does not read the plan yet.
    draft_planner_enabled: bool = False

    # Autonomous corpus updates (every default is safe: off or dry run). pythia/ingest/auto_ingest.py
    # chains discovery, dedupe, ingest, embed, enrich, reindex, temporal re-stamp and the
    # graph-changed drain into one guarded cycle. It is controlled by an on/off toggle in the app,
    # mirrored to a `data/AUTO_INGEST_ENABLED` file (next to KILL_SWITCH and gpu_mps.json). These
    # knobs are read every cycle:
    #   - auto_ingest_enabled: the orchestrator's own master gate. When off, run_cycle returns
    #     {"skipped": "disabled"} before taking the lock or touching the database. run_cycle needs
    #     both this flag and the mirror file to be on, so an environment change alone can never start
    #     mutating a live corpus.
    #   - auto_ingest_dry_run: even when enabled, a cycle only discovers and dedupes and returns the
    #     candidate plan; it never ingests, embeds or reindexes. Going live is a deploy step.
    #   - max_acts_per_cycle / discover_cap: hard per-cycle bounds; the surplus rolls to the next cycle.
    #   - embed_batch / enc_batch: small, memory-safe batch sizes for the automatic path (manual runs
    #     use 256 / 16).
    #   - mem_precheck_ceiling_pct: RAM percentage gate, below the ~90% swap point. At or above it the
    #     cycle skips the heavy embed and reindex stages, leaving acts ingested but not yet embedded
    #     and rolling the embedding to the next cycle.
    #   - lock_ttl_s: TTL of the single-flight run lock (data/AUTO_INGEST.lock); a stale lock is
    #     reclaimed after it expires.
    #   - auto_ingest_publishers: comma-separated publishers the discovery loop watches. The default is
    #     the Collection of Laws; add EU and ČNB sources only once their currency checks are validated.
    #   - snapshot_cmd: operator pg_dump hook run before any amendment. Empty means no snapshot, and
    #     the orchestrator logs a warning if amendments run without one. The path is recorded in the
    #     run row.
    #   - cron_*: the canonical cadence read by the scheduler's discovery and auto-ingest jobs.
    # Turning auto_ingest_enabled off (or deleting the mirror file) stops all automatic updates.
    auto_ingest_enabled: bool = False
    auto_ingest_dry_run: bool = True
    max_acts_per_cycle: int = 5
    discover_cap: int = 50
    embed_batch: int = 64
    enc_batch: int = 8
    mem_precheck_ceiling_pct: float = 80.0
    lock_ttl_s: int = 3600
    auto_ingest_publishers: str = "Sbírka zákonů"  # comma-separated watch list for the discovery loop
    snapshot_cmd: str = ""  # operator pg_dump hook; empty means no snapshot (warns if amendments run)
    # Auto-ingestion cadence as local-time cron fields, kept here so the cron lines and the APScheduler
    # daemon agree: discovery daily at 03:30, auto-ingest weekly on Sunday at 04:30 (off-peak).
    cron_discover: str = "30 3 * * *"
    cron_auto_ingest: str = "30 4 * * 0"

    # LLM provider for the answer pipeline, which gets its adapters from
    # pythia.llm_factory.get_llm(tier):
    #   - "ollama" (default): every tier uses a local OllamaLLMAdapter on ollama_model.
    #   - "anthropic": "generate" uses PaidLLMAdapter on generate_model (the most capable model) and
    #     "cheap" uses cheap_model. Requires anthropic_api_key.
    #   - "openai": "generate" uses OpenAILLMAdapter on openai_generate_model and "cheap" uses
    #     openai_cheap_model. Requires openai_api_key.
    # The provider SDKs are imported lazily inside their adapters, so the default path has no hard
    # dependency on them.
    synthesizer_provider: str = "ollama"  # "ollama" (default) | "anthropic" | "openai"
    # Anthropic model ids, used only when synthesizer_provider == "anthropic". "generate" drafts the
    # answer; "cheap" handles decomposition, the question gate and faithfulness checks (short outputs).
    generate_model: str = "claude-opus-4-8"  # most capable tier, for the answer draft
    cheap_model: str = "claude-haiku-4-5"  # cheap tier for the per-question gate/decompose/verify calls
    # Anthropic API key (secret, never logged). Without it the paid path is unavailable and get_llm
    # falls back to Ollama instead of crashing. Set it via PYTHIA_ANTHROPIC_API_KEY, never in a
    # committed .env file.
    anthropic_api_key: str | None = None
    # OpenAI model ids, used only when synthesizer_provider == "openai"; the tiers are as above.
    openai_generate_model: str = "gpt-5.5"  # most capable tier, for the answer draft
    openai_cheap_model: str = "gpt-5.4-mini"  # cheap tier for the per-question gate/decompose/verify calls
    # OpenAI API key (secret, never logged). Without it the OpenAI path is unavailable and get_llm
    # falls back to Ollama instead of crashing. Set it via PYTHIA_OPENAI_API_KEY, never in a committed
    # .env file.
    openai_api_key: str | None = None

    # Question gate (pythia/reasoning/metagate.py), the core anti-hallucination check. Before
    # generation it classifies a question on three fail-closed axes, in order, first match wins:
    # intent, coverage, premise. An off-topic, out-of-corpus or false-premise question therefore never
    # reaches generation. It runs inside reasoning.answer.answer() only when metagate_enabled is on.
    #   - metagate_enabled (default off): master gate. When off, classify_meta is never called and the
    #     answer carries no `.meta`.
    #   - metagate_mode: "shadow" (default) classifies, attaches `.meta` and logs the route but still
    #     generates the answer as usual, to collect the live route distribution before enforcing.
    #     "enforce" sends the non-sound routes to an honest meta-answer (skipping generation) and the
    #     false_premise route to a correction-led generation.
    # The judges use get_llm("cheap") because they run on every question, refused ones included. The
    # floors are fail-closed thresholds: below its floor, each axis takes the safer route. strong_cos
    # is the raw-cosine bar for the coverage axis's deterministic "strong" signal. All four are
    # calibration thresholds with safe defaults.
    metagate_enabled: bool = False
    metagate_mode: str = "shadow"  # "shadow" (classify and log, still generate) | "enforce" (route)
    intent_floor: float = 0.75  # call a question not-legal only at or above this (rarely rejects)
    coverage_floor: float = 0.6  # below this: weak_coverage (the never-fabricate side)
    premise_floor: float = 0.7  # below this: treat as sound (never a low-confidence correction)
    strong_cos: float = 0.55  # coverage: a raw cosine at or above this is a "strong" on-point hit
    # True-cosine coverage signal (default off, changes the coverage verdict). The coverage axis's
    # deterministic "strong" signal compares a per-provision score against strong_cos. Without this
    # flag that score is cp.score, the structural seed/graph rank (about 2.0 for the top seed), so
    # `top >= strong_cos` always passes and coverage rests entirely on the LLM support judge. That
    # fails safe, so it is a calibration issue rather than a cause of over-refusal. When on, answer.py
    # computes the exact bge-m3 cosine (NexusPort.score_semantic) for the top-k once and feeds it to
    # the deterministic signal, so strong_cos really gates. Keep it off until it has been validated
    # against lawyer-reviewed gold answers and a threshold calibration.
    metagate_true_cosine_enabled: bool = False

    # Per-claim verification loop: atomize the draft into claims, run typed verifiers, record a
    # ledger. Follows the same master/mode pattern as the question gate.
    #   - claim_verify_enabled (default off): master gate. When off, answer() never atomizes or runs a
    #     verifier and adds no audit_trace["claim_ledger"]. The sub-flags below are inert unless it is
    #     on.
    #   - claim_verify_mode: "shadow" (default) atomizes, verifies and attaches the ledger as an audit
    #     annotation without changing the answer, sections, cited provisions or citations_map (every
    #     ledger row gets final_status="shadow"). "enforce" changes the output: the answer is
    #     reassembled from the verified claims. If the enforce assembly cannot be imported, it
    #     degrades to shadow with a logged warning instead of crashing.
    #   - claim_typing_llm_enabled: optional cheap-LLM claim typing that re-types only claims left at
    #     the bare slot default. Reserved; typing is currently deterministic only.
    #   - claim_entailment_llm_enabled: optional cheap-LLM entailment check per claim inside the
    #     citation verifier. Reserved; that verifier is currently a stub that skips.
    #   - claim_calc_worked_example_enabled: lawyer-gated worked-example output for the calculation
    #     verifier, the only sanctioned way to state a computed figure (the verbatim-only rule fails
    #     any computed number or date by default). Reserved; that verifier is currently a stub.
    claim_verify_enabled: bool = False
    claim_verify_mode: str = "shadow"  # "shadow" (verify and attach ledger, output unchanged) | "enforce"
    claim_typing_llm_enabled: bool = False  # sub-flag (inert unless the master is on)
    claim_entailment_llm_enabled: bool = False  # sub-flag (inert unless the master is on)
    claim_calc_worked_example_enabled: bool = False  # sub-flag (inert unless the master is on)
    # Retrieval-gap probe (default off, verification only; also needs claim_verify_enabled). For each
    # claim citation that verify.ground() marked ungrounded (cited but not among the retrieved
    # provisions), the probe asks NexusPort.resolve_citation(ext, sec), at most 8 times per answer,
    # whether that section exists in the corpus at all. If it resolves, the citation is classified
    # `missed_in_corpus`: a retrieval gap, recorded for telemetry and usable as a target by the
    # citation repoint repair. If not, it stays a not-in-corpus / fabricated hard failure. Findings
    # go to audit_trace['verify_evidence'] (the cited tag, missed_in_corpus or not_in_corpus, and the
    # resolved provision id). A missed_in_corpus provision is never added to the served answer's
    # cited_provisions or citations_map. When off, resolve_citation is never called and there is no
    # verify_evidence key.
    claim_verify_gap_probe_enabled: bool = False  # sub-flag (inert unless the master is on)

    # Append-never-displace promotion (default off; needs claim_verify_enabled and
    # claim_verify_gap_probe_enabled, and is meant to be enabled last, after lawyer review). For an
    # ungrounded citation that the probe classified `missed_in_corpus` (the section exists in the
    # corpus but was not retrieved), the provision is appended to the verification evidence, at most
    # `verify_promote_cap` per answer, and verification is re-run over the extended set. Promoted
    # provisions are only added; the original evidence is never removed or reordered. Each promoted
    # chip is labelled promoted=True / needs_lawyer_review=True and recorded in
    # audit_trace['promoted_evidence'].
    claim_verify_promote_enabled: bool = False  # sub-flag (inert unless the master is on); enable last
    verify_promote_cap: int = 2  # max promoted provisions appended per answer

    # Repair loop, verified-only assembly and refusal. All inert unless claim_verify_enabled is on and
    # claim_verify_mode == "enforce".
    #   - claim_repair_max_rounds: ceiling on loop passes (a single claim gets at most one repair).
    #   - claim_survival_floor: the single refusal floor, mirroring verify.py's min_grounding (0.34).
    #     Refuse if all core claims failed or the share of surviving claims is below it.
    #   - claim_repair_strip_enabled: deterministic repair that strips the unverifiable half of a
    #     sentence. When off, the whole sentence is demoted.
    #   - claim_repair_llm_enabled: targeted per-claim LLM prose repair. Off even when the loop is on,
    #     because the loop works fully deterministically. When on it may only re-cite, reframe or
    #     demote (never manufacture support), works on fenced input and is re-verified.
    #   - claim_repair_llm_max_claims: per-answer budget of cheap-LLM redraft calls.
    claim_repair_max_rounds: int = 2
    claim_survival_floor: float = 0.34
    claim_repair_strip_enabled: bool = False
    claim_repair_llm_enabled: bool = False
    claim_repair_llm_max_claims: int = 4

    # Worst-case number of cheap-LLM calls the claim loop may make in one answer turn. It is the single
    # source for reasoning/loop_budget.LoopBudget: the verify lanes (entailment, typing) and the repair
    # lane (redraft) all reserve against one budget built from it. It bounds cost and latency, never
    # the ledger or the answer: instrumentation flags runs that approach it, and the soak test asserts
    # it is never exceeded. The default of 13 is a conservative ceiling (up to N per-claim entailment
    # checks, a few re-types and the worked example) so the loop cannot silently inflate per-answer
    # spend. Read only when claim_verify_enabled is on.
    claim_verify_max_llm_calls: int = 13

    # Deterministic retrieval robustness for vague layman queries: four independent flags, all default
    # off, with no extra LLM call. They only change which and how many candidates the LLM-free seed and
    # retrieve path considers, never refusal, trust, ranking semantics or the answer contract.
    #   - seed_token_idf_enabled: sparse_seed normally keeps the first 14 query tokens of 3+
    #     characters, so in a long vague question a discriminative legal term near the end is dropped.
    #     When on, the terms are ranked by inverse document frequency (from a one-time in-process df
    #     cache over provision.tsv via ts_stat) and the top 14 are kept, ties in original order.
    #   - seed_pool_scale_enabled: lets answer() pass a scaled rerank pool (PythiaQuery.rerank_pool),
    #     so a multi-aspect decomposition widens the shortlist beyond the fixed _RERANK_POOL of 16.
    #     Otherwise hybrid_seed uses rk = max(k * 2, _RERANK_POOL).
    #   - retrieval_concurrent_legs_enabled: run the per-sub-query eng.retrieve calls concurrently
    #     (asyncio.gather) instead of one after another. RRF aggregation is order-independent, so the
    #     merged set and scores are unchanged; only wall-clock time differs.
    #   - retrieval_quotas_enabled: give each decomposition leg a quota before the RRF merge and scale
    #     the final k with the number of legs (k_eff, capped), so a multi-aspect question is not
    #     starved by the global k of 12.
    seed_token_idf_enabled: bool = False
    seed_pool_scale_enabled: bool = False
    retrieval_concurrent_legs_enabled: bool = False
    retrieval_quotas_enabled: bool = False
    # Tuning knobs for the flags above (read only when the matching flag is on).
    seed_pool_cap: int = 48  # ceiling for the scaled rerank pool answer() passes (16 * n_legs)
    retrieval_leg_quota: int = 8  # per-leg top-N kept before the RRF merge when quotas are on
    retrieval_k_eff_cap: int = 24  # ceiling for the leg-scaled effective k when quotas are on
    # Cap on concurrently retrieved legs, so the fan-out cannot exhaust the database pool
    # (db_pool_max_size, default 5). Read only when retrieval_concurrent_legs_enabled is on; the
    # sequential path never creates the semaphore. A failed leg is contained either way.
    answer_leg_concurrency: int = 4  # max legs retrieved at once (keep below db_pool_max_size)

    # Term-normalization retrieval leg (default off, LLM-free, never gates). Layman queries often name
    # something in words with no lexical overlap with the statutory wording (for example "cookies",
    # where the statute says "koncové zařízení", or "švarcsystém" for "závislá práce"), so the
    # sparse_seed tsvector match finds nothing and the one discriminative term never reaches
    # retrieval. When on, term_norm.match_terms() matches the query deterministically (lemma, trigram
    # or exact) against a lexicon built from defined_term.term rows plus a small lawyer-reviewable
    # layman-to-statutory alias file (data/term_aliases.json). Each match (a) adds its statutory
    # term(s) as an extra exact-term-boosted sparse leg and (b) injects the defining provisions
    # (defined_term.definition_provision_id) as extra seeds. It only adds legs and seeds: it never
    # drops, gates or reorders an existing hit. The lexicon is built once per process, not per call.
    term_norm_enabled: bool = False
    term_norm_max_seeds: int = 12  # cap on defining-provision seeds injected per query
    term_norm_trigram_threshold: float = 0.6  # min token-to-lexicon trigram similarity for a fuzzy alias hit

    # Soft-law crowding cap (default off, demote only). Soft law (SOFT_LAW bindingness: úřední
    # sdělení, metodické pokyny, výkladová stanoviska) is useful but must never crowd binding statute
    # out of the final top-k. When on, apply_softlaw_quota() reorders the final k so that at most
    # max_soft = ceil(k / 3) SOFT_LAW provisions remain in the top-k; the excess soft-law hits are
    # demoted below the binding hits. No binding provision is dropped and nothing is removed from the
    # set; it is only reordered. Bindingness is already on the NexusPort row.
    softlaw_quota_enabled: bool = False

    # Unified intake call (default off), the recall lever for vague queries. It replaces answer()'s
    # single cheap-LLM _decompose call with a richer cheap-LLM intake call (reasoning/intake.py) that
    # rewrites the layman question into 2-6 sub-questions in statutory register, plus optional
    # internal HyDE legs and grounded structured facts. The sub-questions feed the existing multi-leg
    # RRF loop (the retrieval robustness flags above compose with it). Facts feed only the answer path
    # and audit_trace, never the app's cascade, candidate or verdict logic.
    #   - intake_v2_enabled (default off): master gate. When off, answer() calls _decompose and there
    #     is no run_intake call and no audit_trace["intake"].
    #   - intake_v2_mode: "shadow" (default) also calls run_intake and logs its result for A/B
    #     comparison, but retrieval still uses the _decompose output, so the answer is unchanged (one
    #     extra cheap call). "active" feeds run_intake's sub_questions into the RRF loop; the raw query
    #     stays a permanent leg (leg 0) and HyDE drafts are added as reduced-weight internal legs.
    intake_v2_enabled: bool = False
    intake_v2_mode: str = "shadow"  # "shadow" (call and log; retrieval uses _decompose) | "active" (feed RRF)
    intake_hyde_weight: float = 0.5  # reduced RRF weight for the internal HyDE legs (active mode)

    # Deterministic intake pre-pass (default off, LLM-free). reasoning/det_intake._det_scan applies a
    # lawyer-reviewable lexicon (pythia/data/intake_lexicon.json: legal-form aliases, gate cues, regime
    # aliases, a small municipality gazetteer) to the raw query and yields IntakeFacts with
    # source='det'. A det fact is a classification, not a copied query span, so it skips the
    # verbatim-span check but its field must still be one of _FACT_FIELDS. It only adds facts: at the
    # merge in intake.run_intake, a det fact that conflicts with an LLM or profile fact on the same
    # field is dropped (add, never override). The flag is read inside run_intake, so it has an effect
    # only when intake_v2_enabled is also on. The lexicon content is a draft pending lawyer review.
    intake_det_scan_enabled: bool = False

    # Engine-side clarify short-circuit (default off). The app already asks a clarifying question
    # before calling the engine when a required slot for the matter type is missing. This is the
    # engine-side counterpart: after retrieval, if the intake still reports something other than
    # 'sufficient' and retrieval is deterministically weak (the top bge-m3 cosine is below
    # intake_clarify_cos_floor), the engine returns a render_mode="clarify" PythiaAnswer
    # (refused=False, verdict=None, drafted_answer="") before the question gate and generation, so a
    # clarified turn spends no generation-tier tokens. It covers only the weak-but-nonempty case; an
    # empty RRF result is still handled by the abstain path just before it.
    #   - intake_clarify_cos_floor: the retrieval-weakness edge. Only max_cos < floor, with an intake
    #     that is not 'sufficient', triggers a clarify.
    # Fail-open: any score_semantic error counts as above the floor, so the answer proceeds. It never
    # reads a question-gate verdict, because the gate has not run yet at that point.
    intake_clarify_enabled: bool = False
    intake_clarify_cos_floor: float = 0.55

    # Confidence bands: presentation-only edges, not a gate. bands.grade() labels an answer that the
    # question gate already cleared (sound or false_premise) as `full` or `hedged`; it never decides
    # whether to answer or refuse. An answer is `full` only when both dials are at or above
    # band_full_floor, otherwise `hedged`. C_retrieval (the raw top cosine) is not calibrated yet, so
    # these are presentation edges only: the worst case of a mis-graded band is an overly cautious
    # amber banner, never a fabricated answer. The defaults (0.60 / 0.40) are conservative fallbacks,
    # to be refit once calibration data exists. The app owns the calibration runtime, so on the engine
    # side these values govern. Read only inside bands.grade(), which runs only with the question gate
    # on.
    band_full_floor: float = 0.60  # both C_retrieval and R_grounding at or above this: `full`, else `hedged`
    band_hedge_floor: float = 0.40  # lower presentation edge (reserved for a future thin/closest split)

    # Evidence bundle and citation resolver (default off, read-only). The citation verifier, the
    # practical-fact and section passes and the essay draft each used to build their own view of a
    # provision's text (a 700-character slice, a ~1600-character context window, the full text), so
    # a cited section could resolve to different text, or none, depending on the pass. The bundle
    # gives them one canonical full-text carrier (contract.EvidenceBundle), built once via
    # NexusPort.fetch_provisions (never raw SQL), so every consumer indexes the same text and every
    # cited section resolves to a concrete provision id and character span. An unresolved citation is
    # reported so the citation verifier can demote it, never silently dropped. The bundle also
    # (a) promotes the adjacent provision(s) when a cited section refers to a neighbouring section or
    # clause that is not in the bundle (bounded, via NexusPort), and (b) snaps a citation of a parent
    # section to the exact subsection or clause span when the text supports it, using the same
    # selection as the essay and practical passes so they never disagree about which text backs a
    # citation.
    # When on (PYTHIA_EVIDENCE_BUNDLE_ENABLED=1), answer() builds the bundle from the rows _fetch
    # already loaded (no extra database call for the carrier) and threads its cited_texts into the
    # claim-verify evidence, so citation support is scored against the full text. Neighbour promotion
    # and clause snapping add a bounded number of provisions via NexusPort (act_tree,
    # fetch_provisions). When off, no bundle is built and every pass reads its own text. The fan-out
    # knobs below are read only when the flag is on.
    evidence_bundle_enabled: bool = False
    evidence_neighbor_fanout: int = 2  # max adjacent sections promoted per unresolved cited act (each side)
    evidence_max_promoted: int = 12  # hard cap on neighbour-promoted provisions per answer turn
    # Hard cap on the internal full-text carrier per EvidenceProvision, so one huge provision cannot
    # push unbounded text into verification. Separate from wire redaction: the carrier text is never
    # serialized onto /v1/answer, whatever this cap is (see the JSON field serializer on
    # EvidenceProvision.text). Consumers index spans into text[:cap].
    verify_fulltext_cap: int = 20000

    # Per-stage latency instrumentation for the answer pipeline (default off, observe only). When on,
    # reasoning/answer.py times each stage (intake, retrieve, generate, verify, assemble) with
    # time.perf_counter and records it via telemetry.record_stage_latency; metrics() exposes the
    # result as 'stage_latency_ms': {stage: {p50, p95, max, last}}. It never enforces a budget, changes
    # a path or touches the answer content. (Reading a clock is fine here: this is runtime telemetry,
    # not the pure audit core.) Separate from the claim loop's own stage latency, which is recorded
    # only when claim verification is on.
    stage_latency_enabled: bool = False

    # Answer-quality tripwires (default off, observe only). When on, reasoning/answer.py collects
    # signals the assembled answer already has (grounded citation count, verdict, hedge flag, empty
    # practical block, claim-ledger demoted/dropped counts) and runs the pure evaluator
    # reasoning/quality_tripwires.evaluate() against the thresholds below. Each crossed tripwire emits
    # an 'answer.quality.tripwire' telemetry event (which tripwire and its value) and bumps a /metrics
    # counter. It never raises, demotes or refuses; it is a monitoring signal only. A None int
    # threshold or a False bool threshold disables that tripwire.
    quality_tripwires_enabled: bool = False
    tripwire_max_demoted_slots: int = 3  # fire when the claim loop demoted > this many slots to gap lines
    tripwire_max_dropped_claims: int = 2  # fire when the loop dropped > this many claims (safety/unverifiable)
    tripwire_zero_grounded: bool = True  # fire when an OK answer grounded 0 citations
    tripwire_empty_practical: bool = True  # fire when a practical action-plan block was produced but empty
    tripwire_hedged: bool = True  # fire when the answer was presented hedged (amber)

    # Local LLM via Ollama (no paid key): answer generation and verification, and synthetic training
    # questions.
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:14b"  # sized for a 24 GB M5 Pro; strong Czech and reasoning
    llm_temperature: float = 0.2
    llm_seed: int = 42  # fixed RNG seed for reproducible generation (same question and model, same answer)
    llm_timeout_s: float = 180.0  # generous: the first call loads the 14B model into memory

    # How long Ollama keeps the model resident after a request (the `keep_alive` field of /api/chat).
    # It affects latency and memory only, never output: the same prompt, model, temperature and seed
    # give the same tokens whether the weights were warm or had to be reloaded. The 14B model is large
    # (about 9-10 GB resident) and is loaded only on the /v1/answer path; with Ollama's default of 5
    # minutes it would hold that memory long after a single answer. "30s" unloads it soon after an
    # answer without reloading in the middle of one (decompose, generate and verify all run within
    # about 30 s). PYTHIA_OLLAMA_KEEP_ALIVE accepts any Ollama duration: "5m", "10m", "0" (unload right
    # after the call) or "-1" (keep it loaded forever).
    ollama_keep_alive: str = "30s"

    # Bounds of the process-wide asyncpg pool: min_size connections are opened eagerly and held idle,
    # max_size is the ceiling. They affect resource footprint only, never query results. db.get_pool
    # floors min_size at 1 so a bad override cannot produce an unusable pool. Override with
    # PYTHIA_DB_POOL_MIN_SIZE / PYTHIA_DB_POOL_MAX_SIZE.
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5


def get_settings() -> PythiaSettings:
    return PythiaSettings()


# Boot-time environment manifest. When AQ_ENV_MANIFEST_DIR is set, write_env_manifest dumps the fully
# resolved PythiaSettings (secrets redacted) as JSON at startup, so the exact configuration a run
# booted with is recorded as an artifact rather than only in a runner script. Unset by default.
_SECRET_KEY_MARKERS = ("api_key", "password", "secret", "token")
_URL_KEY_MARKERS = ("url", "dsn")


def _redact_settings(dump: dict) -> dict:
    """Return a copy of a settings dump with secrets removed.

    Keys that look secret are fully redacted; URL/DSN keys keep host and database but lose any
    ``user:pass@`` userinfo. Pure; never raises on odd values.
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


def write_env_manifest(*, component: str = "engine") -> str | None:
    """Write the resolved settings (secrets redacted) to ``$AQ_ENV_MANIFEST_DIR`` at boot.

    Returns the written path, or None when the variable is unset or empty. Fail-soft: any error is
    swallowed, because a manifest write must never break startup.
    """
    import json
    import os

    target = os.environ.get("AQ_ENV_MANIFEST_DIR")
    if not target or not target.strip():
        return None
    try:
        d = Path(target.strip())
        d.mkdir(parents=True, exist_ok=True)
        payload = _redact_settings(get_settings().model_dump())
        path = d / f"pythia-{component}-env-manifest.json"
        path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True))
        return str(path)
    except Exception:  # noqa: BLE001 — a manifest write must never break boot
        return None
