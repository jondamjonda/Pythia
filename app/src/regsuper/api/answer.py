"""Customer answer endpoint (`POST /api/answer`): one legal question in, a cited answer out.

The Pythia engine drafts the answer and runs the question gate; this router authenticates, meters,
calls the engine and shapes the result into `AnswerResponse` without ever adding a claim. Every
chip must cite a provision in the engine's grounded set, or it is dropped; if no chip survives,
the answer is refused. Returns 404 unless `answer_endpoint_enabled` is set (off by default).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from regsuper.analysis import authority_resolver as authority
from regsuper.analysis import clarify as clarify_mod
from regsuper.analysis.answer_context import AnswerContext
from regsuper.analysis.compliance_guard import classify_answer_mode
from regsuper.api import route_copy
from regsuper.api.auth import Principal, get_principal
from regsuper.conduit.envelope import RefusalReason
from regsuper.config import get_settings
from regsuper.entitlements import metering
from regsuper.entitlements.guard import require_entitlement
from regsuper.ingest import authority_registry as auth_registry
from regsuper.observability import events
from regsuper.observability import ledger_export as _ledger_export
from regsuper.observability.killswitch import KillSwitchEngaged, assert_not_killed
from regsuper.observability.limits import SpendLimitExceeded, enforce_spend_limit
from regsuper.observability.logging_config import get_logger
from regsuper.pythia import client as pythia
from regsuper.storage.database import session_scope

router = APIRouter(prefix="/api/answer", tags=["answer"])
_log = get_logger(__name__)

# The corpus the answer is grounded in. The engine answers from its own `pythia` database, so every
# chip id is a Pythia corpus id; it is stamped on each chip so the frontend's source viewer
# resolves it via /sources/{id}?corpus=pythia.
_CORPUS_PYTHIA = "pythia"

# The metered unit: one question is one daily `queries` unit. An engine answer is not the
# multi-layer analysis cascade, so it does not consume `analyses`.
_METRIC_QUERIES = "queries"

# Provider name under which the engine's LLM spend is tracked on the app's cost rows (see
# pythia.client). The engine spends on its own LLM key, so the spend checks below enforce this
# provider's caps, not the 'openai' caps: the answer endpoint never spends a direct OpenAI key.
_PROVIDER_ENGINE_LLM = "pythia_engine_llm"

# Worst-case projected engine spend for one answer, in USD. The engine caps its claim
# verify-and-repair loop at 13 LLM calls (`claim_verify_max_llm_calls`), so the worst case is the
# drafting call plus 13 loop calls, each on GPT-5.5 ($5 per 1M input and $30 per 1M output tokens,
# per data/model_prices_default.json). At a conservative 8k input and 2k output tokens per call,
# that is $0.04 + $0.06 = $0.10 per call, or about $1.40 for 14 calls, rounded up to $1.50.
# Over-projecting is the safe direction: the check refuses slightly early, never late. This is
# the projection the spend check adds to the day's spend before calling the engine, not a charge.
_ANSWER_ENGINE_WORST_CASE_COST_USD = 1.50

# Engine render mode to app render mode. The four non-sound modes map one-to-one to the
# route_copy frames; the two sound modes render the drafted sections (hedged ones with a banner).
# An unknown engine mode degrades to the safest frame (refuse) instead of crashing.
_ENGINE_TO_APP_RENDER_MODE: dict[str, str] = {
    "full": "full",
    "hedged": "hedged",
    "closest": "closest",  # not_in_corpus
    "notlegal": "notlegal",  # not_a_legal_question
    "falsepremise": "falsepremise",
    "refuse": "refuse",  # rejected by post-generation verification
    # The engine's own clarify request (intake_clarify_enabled). Deliberately absent from
    # _RENDER_MODE_TO_ROUTE and _NON_SOUND_MODES: a clarify is not a refusal but a bounded request
    # for more facts (refused=False), so _shape_answer routes it to the clarify branch.
    "clarify": "clarify",
}

# App render mode to route_copy key. Only the non-sound frames have copy; a sound answer renders
# its sections, and a hedged one fetches the hedge banner separately.
_RENDER_MODE_TO_ROUTE: dict[str, str] = {
    "notlegal": route_copy.ROUTE_NOT_A_LEGAL_QUESTION,
    "closest": route_copy.ROUTE_NOT_IN_CORPUS,
    "falsepremise": route_copy.ROUTE_FALSE_PREMISE,
    "refuse": route_copy.ROUTE_REFUSE,
}

# App render mode to RefusalReason, the machine-readable code that API integrators switch on,
# exposed on `AnswerResponse.reason`. Only non-sound frames and clarify requests carry a reason.
_RENDER_MODE_TO_REFUSAL_REASON: dict[str, RefusalReason] = {
    "notlegal": RefusalReason.OUT_OF_SCOPE,
    "closest": RefusalReason.NO_COVERAGE,
    "falsepremise": RefusalReason.FALSE_PREMISE,
    "refuse": RefusalReason.NO_COVERAGE,
    # A clarify is not a refusal (refused=False), but it still carries a `reason` so an integrator
    # can switch on it and render a form. It is not in _NON_SOUND_MODES, so `refused` stays False.
    "clarify": RefusalReason.NEEDS_CLARIFICATION,
}

_NON_SOUND_MODES = frozenset(_RENDER_MODE_TO_ROUTE)

# The clarify render mode: neither an error nor a refusal, but a request for one round of missing
# facts. It has `refused=False` and its own route, and is not in _NON_SOUND_MODES (those are the
# engine's non-answer frames). The frontend shows a clarify frame as a form (render the questions,
# resubmit with `slots`) rather than a refusal banner. It carries the information-not-advice
# disclaimer and a line recommending a lawyer.
#
# Two sources produce this frame:
#   * App-side clarify (answer_clarify_enabled): deterministic and before the engine. On the first
#     turn, when a required slot for the detected matter type is missing, the handler returns this
#     frame before quota and engine (nothing reserved, no LLM call).
#   * Engine-side clarify (the engine's intake_clarify_enabled): after retrieval, when the intake
#     is still insufficient and retrieval is weak, the engine returns render_mode="clarify" with an
#     empty draft. _shape_answer maps it to the same frame, never downgrading it to a refusal, and
#     the handler refunds the reserved `queries` unit, since nothing billable was drafted.
#
# The two never occur on the same turn: an app-side clarify returns before the engine runs. Nor
# can an engine clarify collide with answer_shadow_weak_coverage_hedged (a separate post-engine
# upgrade from closest to hedged), because an engine that returned "clarify" returned no "closest".
_RENDER_MODE_CLARIFY = "clarify"

# Copy for the clarify frame (Czech is primary; seed wording pending legal review). The questions
# themselves come from the fixed clarify.CLARIFY_CATALOG, so an LLM never writes copy. The
# frame bypasses the route_copy path, so title, body and disclaimer are set explicitly, and the
# lawyer referral is part of the body so it always renders.
_CLARIFY_TITLE: dict[str, str] = {
    "cs": "Potřebujeme upřesnit několik údajů",
    "en": "We need to clarify a few details",
}
_CLARIFY_BODY: dict[str, str] = {
    "cs": (
        "Abychom odpověděli přesně a neuváděli nepodložené závěry, potřebujeme nejdřív "
        "upřesnit několik údajů k vaší situaci. Odpovězte prosím na následující otázky. "
        "Pro závazné posouzení vašeho konkrétního případu se obraťte na advokáta."
    ),
    "en": (
        "To answer accurately and avoid unsupported conclusions, we first need to clarify a "
        "few details about your situation. Please answer the questions below. For a binding "
        "assessment of your specific case, consult a lawyer."
    ),
}
# The lawyer-referral line on its own (it is also part of the body above), exposed on ``reason``
# as a machine-readable referral signal on the clarify frame.
_CLARIFY_REFERRAL: dict[str, str] = {
    "cs": "Pro závazné posouzení vašeho konkrétního případu se obraťte na advokáta.",
    "en": "For a binding assessment of your specific case, consult a lawyer.",
}


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------
class AskAnswerRequest(BaseModel):
    """Request body: exactly one self-contained ``query`` (the endpoint is stateless).

    Unknown fields such as ``history``, ``messages`` or ``context`` are rejected with 422.
    Conversation history is never folded into the query, because no tested resolver for it
    exists and concatenating it would open an ungrounded prompt-injection path. ``lang`` selects
    the language of the frame copy (Czech by default). ``as_of`` requests a point-in-time answer.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    as_of: date | None = None
    project_id: str | None = None
    lang: str = "cs"
    # Answers to clarify questions. The first turn has no `slots` and may get a clarify frame;
    # the follow-up turn carries the user's answers and is never asked to clarify again (at most
    # one round). The dict is validated against the slot enums in the handler.
    slots: dict[str, Any] | None = None


class ChipOut(BaseModel):
    """One clickable citation chip over the drafted answer.

    ``provision_id`` is always in the engine's grounded ``cited_provisions``. ``corpus`` is
    ``"pythia"``, so the source can be opened via /sources/{id}?corpus=pythia.
    """

    provision_id: str
    label: str | None = None
    corpus: str = _CORPUS_PYTHIA
    span: list[int] | None = None  # [start, end] character offsets in the draft, or None


class SectionOut(BaseModel):
    """One section of the drafted answer (Legislativní rámec, Judikatura, Hodnocení).

    ``chips`` holds the grounded chips that fall inside this section.
    """

    kind: str
    heading: str
    text: str
    chips: list[ChipOut] = Field(default_factory=list)


class ClarifyQuestionOut(BaseModel):
    """One clarifying question for the frontend's form.

    ``slot`` is the key to send back in the follow-up ``slots`` dict; ``text`` is fixed copy
    from the question catalogue, never written by an LLM.
    """

    slot: str
    text: str


class PracticalItemOut(BaseModel):
    """One practical fact from the engine's structured block (mirrors the engine's PracticalItem).

    ``provision_id`` is always in the grounded cited-provision set: an item citing an ungrounded
    provision is dropped and its ``slot`` recorded in the block's ``demoted_slots``.
    ``value_raw`` is copied verbatim from the provision.
    """

    slot: str
    fact_type: str
    value_raw: str | None = None
    provision_id: str | None = None
    grounded: bool = True


class PracticalBlockOut(BaseModel):
    """Structured practical facts (deadlines, authorities, ...) mirrored from the engine's answer.

    Set only when the engine returned a consistent practical block and
    ``answer_practical_block_enabled`` is on (off by default). ``items`` are the grounded facts
    that passed the citation check. ``gaps`` (explicit non-claims for data the corpus lacks, and
    claim slots demoted by verification) and ``demoted_slots`` pass through unchanged, since they
    cite nothing. ``profile_used`` records whether a business profile supplied the situation.
    The sections and chips remain the complete answer; rendering this block is optional.
    """

    items: list[PracticalItemOut] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    demoted_slots: list[str] = Field(default_factory=list)
    profile_used: bool = False


class MetaOut(BaseModel):
    """The engine's question-gate verdict, for transparency.

    None unless the engine ran the gate (its metagate_enabled setting). Informational only: it
    changes no chip and grounds no claim.
    """

    route: str | None = None
    intent: str | None = None
    coverage: str | None = None
    premise: str | None = None
    reason: str | None = None


class IntakeFactOut(BaseModel):
    """One fact the engine's intake extracted from the question, from a closed set of fields.

    ``field`` is the key, ``value`` the extracted value and ``source`` how it was resolved
    (``det``, ``llm`` or ``profile``). No other free text is passed through.
    """

    field: str
    value: str
    source: str | None = None


class IntakeDroppedOut(BaseModel):
    """One proposed fact the intake verifier dropped ("co jsme nevzali v potaz", not considered).

    ``field`` is the dropped key and ``reason`` the machine reason (seed copy pending legal
    review). No other free text is passed through.
    """

    field: str
    reason: str | None = None


class IntakeConflictOut(BaseModel):
    """One conflict the intake flagged: a field for which the question gave contradictory values.

    ``field`` is in ``_INTAKE_FACT_WHITELIST``; ``values`` are only the stringified candidate
    values, taken from a closed set of keys (free-text keys such as ``detail``, ``message`` or
    ``note`` never pass through). Informational only.
    """

    field: str
    values: list[str] = Field(default_factory=list)


class IntakeOut(BaseModel):
    """How the engine understood the question, for the "understanding" panel.

    Set only when ``answer_intake_panel_enabled`` is on and the engine returned
    ``audit_trace["intake"]``. Contains only whitelisted data: the ``sub_questions``, the
    extracted ``facts`` (with their source), the ``conflicts`` the intake flagged, a
    ``sufficiency`` verdict and the ``dropped`` facts with reasons. It grounds no claim and changes
    no chip.
    """

    sub_questions: list[str] = Field(default_factory=list)
    facts: list[IntakeFactOut] = Field(default_factory=list)
    conflicts: list[IntakeConflictOut] = Field(default_factory=list)
    sufficiency: str | None = None
    dropped: list[IntakeDroppedOut] = Field(default_factory=list)


class AuthorityOut(BaseModel):
    """One specific competent office for the practical "kam se obrátit" (where to turn) slot.

    ``authority_type`` is the shared vocabulary tag, ``name`` the office's display name from the
    registry seed, ``match_kind`` how it matched ('national', or okres, orp or kraj) and
    ``is_draft`` is always True while the seed awaits legal review. There is deliberately no
    address, data box or source URI: contact details belong to the operator's validated directory,
    and nothing here is persisted into ``api_call.request_json``. Offices are listed only when
    ``authority_registry_enabled`` is on and a loaded seed plus a known municipality and topic
    resolve one; an office is never guessed.
    """

    authority_type: str
    name: str
    match_kind: str
    is_draft: bool = True


class ProfileSuggestionOut(BaseModel):
    """A suggestion to save one of the user's own intake facts to their company profile.

    Shown with a one-click "uložit do profilu" (save to profile) button and never written
    automatically: the frontend calls the profiles API only on an explicit user action.
    """

    field: str
    value: str


class AnswerResponse(BaseModel):
    """The shaped answer.

    ``refused`` distinguishes a drafted answer (sections and chips) from a non-answer frame, whose
    copy is in ``title``, ``body`` and ``disclaimer``. Every response carries a ``disclaimer``.
    """

    sections: list[SectionOut] = Field(default_factory=list)
    chips: list[ChipOut] = Field(default_factory=list)
    meta: MetaOut | None = None
    band: str | None = None
    render_mode: str | None = None
    refused: bool = False
    reason: str | None = None
    disclaimer: str = ""
    # Copy for a non-answer frame, or the hedge banner on a hedged answer. Empty on a full answer.
    title: str = ""
    body: str = ""
    hedge_banner: str = ""
    # The "datum účinnosti neuvedeno" (effective date not stated) caveat, set when a cited
    # provision has no recorded effective date; a date is never invented.
    as_of_unknown_note: str = ""
    # Up to 3 clarifying questions on a `render_mode="clarify"` frame (empty otherwise).
    # ``matter_type`` records the detected matter type for transparency and audit.
    questions: list[ClarifyQuestionOut] = Field(default_factory=list)
    matter_type: str | None = None
    # Assumptions the answer hedged over. When a follow-up turn still leaves required slots empty,
    # the engine answers hedged instead of asking again; this lists the missing slot names so the
    # reader sees what was assumed. Informational only: it grounds no claim and changes no chip.
    assumptions: list[str] = Field(default_factory=list)
    # The engine's structured practical block, citation-checked (see PracticalBlockOut). None when
    # the engine omits it or answer_practical_block_enabled is off.
    practical: PracticalBlockOut | None = None
    # How the engine understood the question (see IntakeOut). None when the engine omits it or
    # answer_intake_panel_enabled is off.
    intake: IntakeOut | None = None
    # Suggestions to save the user's own whitelisted intake facts to their profile. Never saved
    # automatically; the frontend offers a one-click save.
    profile_suggestions: list[ProfileSuggestionOut] = Field(default_factory=list)
    # Specific competent offices for the "kam se obrátit" (where to turn) slot (see AuthorityOut).
    # Filled only when ``authority_registry_enabled`` is on, a registry seed is loaded, and a known
    # municipality and mapped topic resolve at least one office; never a guessed office.
    authorities: list[AuthorityOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Feature flag: 404 unless enabled (same pattern as export and digest)
# ---------------------------------------------------------------------------
def _require_enabled() -> None:
    """Raise 404 unless ``answer_endpoint_enabled`` is set (it is off by default).

    A 404 rather than a 403, so a disabled endpoint is indistinguishable from a missing one.
    """
    if not get_settings().answer_endpoint_enabled:
        raise HTTPException(status_code=404, detail="not found")


# ---------------------------------------------------------------------------
# Profile context: the small, whitelisted profile dict sent to the engine
# ---------------------------------------------------------------------------
def _serialize_answer_context(ctx: AnswerContext) -> dict[str, Any]:
    """Serialise an AnswerContext to the whitelisted dict sent to the engine as ``project_context``.

    Only typed scoping fields are included: legal_form, nace_code, derived_tags, headcount_band,
    turnover_band, jurisdiction, the five compliance flags, municipality_code and
    preferred_language. No free text or personal data (display name, legal name, IČO, turnover
    amount, address) ever leaves the app. This function is the single owner of that wire shape;
    every key is always present (with typed defaults), so the engine's known-facts prompt section
    stays stable.
    """
    return {
        "legal_form": ctx.legal_form,
        "nace_code": ctx.nace_code,
        "derived_tags": list(ctx.derived_tags),
        "headcount_band": ctx.headcount_band,
        "turnover_band": ctx.turnover_band,
        "jurisdiction": ctx.jurisdiction,
        "processes_personal_data": ctx.processes_personal_data,
        "handles_client_money": ctx.handles_client_money,
        "has_real_estate": ctx.has_real_estate,
        "cross_border": ctx.cross_border,
        "is_employer": ctx.is_employer,
        "municipality_code": ctx.municipality_code,
        "preferred_language": ctx.preferred_language,
    }


async def _load_answer_context(project_id: str, org_id: Any) -> AnswerContext | None:
    """Return the AnswerContext of the business profile linked to ``project_id``, or None.

    Returns None for a malformed project_id, a project not owned by ``org_id`` (an app-level
    check on top of row-level security), a project without a linked profile, a missing or
    deleted profile, or any load error. Never raises: a failed profile load must fall back to
    answering without context rather than break the question. Used by the app-side clarify
    (to resolve slots) and by the profile context sent to the engine.
    """
    from uuid import UUID

    from regsuper.storage.models import BusinessProfile, Project

    try:
        pid = UUID(str(project_id))
    except (TypeError, ValueError):
        return None
    try:
        async with session_scope() as session:
            project = await session.get(Project, pid)
            # Row-level security enforces tenancy when enabled; this app-level check also holds
            # with rls_enforced=False (the default), so another org's project never leaks a profile.
            if project is None or getattr(project, "org_id", None) != org_id:
                return None
            bp_id = getattr(project, "business_profile_id", None)
            if bp_id is None:
                return None
            profile = await session.get(BusinessProfile, bp_id)
            if profile is None or getattr(profile, "org_id", None) != org_id:
                return None
            if (getattr(profile, "metadata_", None) or {}).get("deleted_at") is not None:
                return None
            return AnswerContext.from_business_profile(profile)
    except Exception:  # a failed profile load falls back to answering without context
        _log.warning("answer_profile_context_load_failed", project_id=str(project_id))
        return None


async def _load_profile_context(project_id: str, org_id: Any) -> dict[str, Any] | None:
    """Return the whitelisted profile-context dict for the engine, or None if there is none.

    A thin wrapper over :func:`_load_answer_context` and :func:`_serialize_answer_context`.
    """
    ctx = await _load_answer_context(project_id, org_id)
    return _serialize_answer_context(ctx) if ctx is not None else None


# ---------------------------------------------------------------------------
# Competent authority for the "kam se obrátit" (where to turn) slot; off by default
# ---------------------------------------------------------------------------
# The committed sample topic map and gazetteer (pending legal review). They contain no office data,
# only the topic-to-authority-type map and the municipality gazetteer, and are used when the
# operator has not configured a path. The office registry seed, by contrast, defaults to empty,
# so enabling the flag alone never produces a guessed office.
#
# In-process cache of the resolver inputs (AuthorityIndex, topic_map, gazetteer_by_code), built
# once per process from the configured local files. (None, None, None) is a valid cached "no
# registry" value, so _AUTH_REGISTRY_BUILT separately records whether the build has happened.
# Building never fetches anything and never touches the database.
_SAMPLE_TOPIC_MAP = authority._SAMPLE_TOPICS
_SAMPLE_GAZETTEER = authority._SAMPLE_GAZETTEER

_AUTH_REGISTRY_CACHE: tuple[
    authority.AuthorityIndex | None,
    dict[str, tuple[str, ...]] | None,
    dict[str, dict[str, Any]] | None,
] = (None, None, None)
_AUTH_REGISTRY_BUILT = False

# Best-effort mapping (pending legal review) from the intake's ``obligation_type`` and ``sector``
# facts, or as a coarse fallback the gate's ``intent``, to a topic_key in the topic-to-authority
# map. Deliberately conservative: an unmapped obligation yields no topic_key and therefore no
# office, never a guessed one. A lawyer must validate it before it drives customer-facing output;
# it exists so the enabled path has a deterministic mapping to test against.
_OBLIGATION_TO_TOPIC: dict[str, str] = {
    "tax": "dan_z_prijmu",
    "vat": "dph",
    "dph": "dph",
    "social_insurance": "socialni_pojisteni",
    "health_insurance": "socialni_pojisteni",
    "occupational_safety": "bezpecnost_prace",
    "hygiene": "hygiena_provozovny",
    "trade_licence": "zivnostenske_opravneni",
    "data_protection": "gdpr",
    "gdpr": "gdpr",
    "employment": "zamestnavani",
}


def _reset_authority_registry_cache() -> None:
    """Clear the in-process registry cache so a test can rebuild it with other settings.

    Never called on the request path.
    """
    global _AUTH_REGISTRY_CACHE, _AUTH_REGISTRY_BUILT
    _AUTH_REGISTRY_CACHE = (None, None, None)
    _AUTH_REGISTRY_BUILT = False


def _build_authority_registry() -> tuple[
    authority.AuthorityIndex | None,
    dict[str, tuple[str, ...]] | None,
    dict[str, dict[str, Any]] | None,
]:
    """Build ``(index, topic_map, gazetteer)`` from the configured local files.

    Reads local files only (no network, no database). A read or parse failure degrades that
    part to empty, so the slot resolves nothing rather than breaking the answer.

    * Office seed: ``authority_seed_path`` if set and present, otherwise an empty index. The
      sample seed is never used on the request path, so nothing is shown until the operator
      points at a real registry.
    * Topic map and gazetteer: the configured path if set and present, otherwise the committed
      sample (no office data, pending legal review).
    """
    import json as _json

    settings = get_settings()
    # Office index: empty unless the operator configured a seed file.
    index: authority.AuthorityIndex | None = None
    seed_path = getattr(settings, "authority_seed_path", None)
    try:
        if seed_path and Path(seed_path).is_file():
            index = authority.build_index(auth_registry.load_seed(seed_path))
        else:
            # Empty index: without a configured seed no office is ever guessed.
            index = authority.build_index(auth_registry.parse_seed([]))
    except Exception:  # a malformed seed degrades to no offices, never breaks the answer
        _log.warning("answer_authority_seed_load_failed", seed_path=str(seed_path))
        index = authority.build_index(auth_registry.parse_seed([]))

    # Topic map.
    topic_map: dict[str, tuple[str, ...]] | None = None
    tm_path = getattr(settings, "authority_topic_map_path", None)
    try:
        tm_src = Path(tm_path) if (tm_path and Path(tm_path).is_file()) else _SAMPLE_TOPIC_MAP
        topic_map = authority.load_topic_map(tm_src)
    except Exception:  # a malformed topic map degrades to no mapping, never breaks the answer
        _log.warning("answer_authority_topic_map_load_failed")
        topic_map = {}

    # Gazetteer: municipality_code to a row with its okres, ORP and kraj area codes.
    gazetteer: dict[str, dict[str, Any]] = {}
    gz_path = getattr(settings, "authority_gazetteer_path", None)
    try:
        gz_src = Path(gz_path) if (gz_path and Path(gz_path).is_file()) else _SAMPLE_GAZETTEER
        rows = _json.loads(Path(gz_src).read_text(encoding="utf-8"))
        gazetteer = {str(r["code"]): r for r in rows if isinstance(r, dict) and r.get("code")}
    except Exception:
        # A malformed gazetteer degrades to no municipality lookup; it never breaks the answer.
        _log.warning("answer_authority_gazetteer_load_failed")
        gazetteer = {}

    return index, topic_map, gazetteer


def _authority_registry() -> tuple[
    authority.AuthorityIndex | None,
    dict[str, tuple[str, ...]] | None,
    dict[str, dict[str, Any]] | None,
]:
    """Return the cached ``(index, topic_map, gazetteer)``, building it on first use.

    Memoised, so requests never re-read the local files.
    """
    global _AUTH_REGISTRY_CACHE, _AUTH_REGISTRY_BUILT
    if not _AUTH_REGISTRY_BUILT:
        _AUTH_REGISTRY_CACHE = _build_authority_registry()
        _AUTH_REGISTRY_BUILT = True
    return _AUTH_REGISTRY_CACHE


def _derive_topic_key(ans: dict[str, Any]) -> str | None:
    """Derive a topic_key from the engine answer (best effort, pending legal review).

    Deterministic and never raises. Reads the intake facts ``obligation_type`` and then
    ``sector``, and as a last resort the gate's ``intent``, and maps them through
    ``_OBLIGATION_TO_TOPIC``. An unmapped value yields None, so no office is guessed.
    """
    facts = (
        ((ans.get("audit_trace") or {}).get("intake") or {}).get("facts")
        if isinstance(ans.get("audit_trace"), dict)
        else None
    )
    if isinstance(facts, list):
        for key in ("obligation_type", "sector"):
            for f in facts:
                if not isinstance(f, dict) or f.get("field") != key:
                    continue
                val = f.get("value")
                topic = (
                    _OBLIGATION_TO_TOPIC.get(str(val).strip().lower()) if val is not None else None
                )
                if topic:
                    return topic
    meta = ans.get("meta")
    if isinstance(meta, dict):
        intent = meta.get("intent")
        if isinstance(intent, str):
            topic = _OBLIGATION_TO_TOPIC.get(intent.strip().lower())
            if topic:
                return topic
    return None


def _derive_municipality_code(ans: dict[str, Any], ctx: AnswerContext | None) -> str | None:
    """Return the asker's municipality code: from the profile if loaded, else from the intake facts.

    None when neither has one; then only national offices can match.
    """
    if ctx is not None and getattr(ctx, "municipality_code", None):
        return str(ctx.municipality_code)
    facts = (
        ((ans.get("audit_trace") or {}).get("intake") or {}).get("facts")
        if isinstance(ans.get("audit_trace"), dict)
        else None
    )
    if isinstance(facts, list):
        for f in facts:
            if isinstance(f, dict) and f.get("field") == "municipality_code":
                val = f.get("value")
                if val is not None and str(val).strip():
                    return str(val).strip()
    return None


def _resolve_authorities(ans: dict[str, Any], ctx: AnswerContext | None) -> list[AuthorityOut]:
    """Resolve the specific competent offices for the answer, as whitelisted ``AuthorityOut`` items.

    Returns [] unless ``authority_registry_enabled`` is on, and also whenever something is missing
    (empty registry, no municipality, unmapped topic): an office is never guessed. The work is done
    by the pure ``maybe_resolve_authorities``, which needs no database and never raises.

    This is the only code path for the "kam se obrátit / který úřad" (which office) answer: it
    resolves through ``authority_resolver.resolve`` over the seeded ``authority`` and
    ``authority_jurisdiction`` data, and there is no parallel office block.
    """
    if not getattr(get_settings(), "authority_registry_enabled", False):
        return []
    index, topic_map, gazetteer = _authority_registry()
    topic_key = _derive_topic_key(ans)
    muni_code = _derive_municipality_code(ans, ctx)
    municipality = (gazetteer or {}).get(muni_code) if muni_code else None
    resolved = authority.maybe_resolve_authorities(
        topic_key, municipality, index, topic_map, enabled=True
    )
    return [
        AuthorityOut(
            authority_type=r.authority_type,
            name=r.name,
            match_kind=r.match_kind,
            is_draft=bool(r.is_draft),
        )
        for r in resolved
    ]


# ---------------------------------------------------------------------------
# Response shaping (pure, no I/O): turns the engine's answer dict and the request language into an
# AnswerResponse, enforcing the citation check.
# ---------------------------------------------------------------------------
def _cited_provision_ids(ans: dict[str, Any]) -> set[str]:
    """Return the grounded provision ids; every emitted chip must cite one of them.

    The base is the engine's verified ``cited_provisions``. On a non-sound route the gate also
    attaches retrieved, grounded ids in ``meta.closest_provisions`` and
    ``meta.correcting_citations``; those are admitted too, so a "not in corpus" closest list or a
    false-premise correction can show real chips.
    """
    ids: set[str] = set()
    for cp in ans.get("cited_provisions") or []:
        pid = (cp or {}).get("provision_id") if isinstance(cp, dict) else None
        if pid:
            ids.add(str(pid))
    meta = ans.get("meta") or {}
    if isinstance(meta, dict):
        for key in ("closest_provisions", "correcting_citations"):
            for cp in meta.get(key) or []:
                pid = (cp or {}).get("provision_id") if isinstance(cp, dict) else None
                if pid:
                    ids.add(str(pid))
    return ids


def _chip_from_map(entry: dict[str, Any], grounded_ids: set[str]) -> ChipOut | None:
    """Map one ``citations_map`` entry to a ChipOut, or None if its provision is not grounded."""
    if not isinstance(entry, dict):
        return None
    pid = entry.get("provision_id")
    if not pid or str(pid) not in grounded_ids:
        return None  # forged or ungrounded citation: drop it
    start, end = entry.get("start"), entry.get("end")
    span = [int(start), int(end)] if isinstance(start, int) and isinstance(end, int) else None
    return ChipOut(
        provision_id=str(pid),
        label=entry.get("label"),
        corpus=_CORPUS_PYTHIA,  # answers always come from the Pythia corpus
        span=span,
    )


def _has_null_valid_from(ans: dict[str, Any]) -> bool:
    """Return True if any cited provision has no effective date (valid_from).

    Such provisions are treated as in force, and the answer shows a caveat instead of a date.
    """
    for cp in ans.get("cited_provisions") or []:
        if not isinstance(cp, dict):
            continue
        # The provision counts as in force; a missing valid_from or effective_from triggers the
        # caveat.
        vf = cp.get("valid_from", cp.get("effective_from", "__present__"))
        if vf in (None, "", "null"):
            return True
    return False


def _meta_out(ans: dict[str, Any]) -> MetaOut | None:
    meta = ans.get("meta")
    if not isinstance(meta, dict) or not meta:
        return None
    return MetaOut(
        route=meta.get("route"),
        intent=meta.get("intent"),
        coverage=meta.get("coverage"),
        premise=meta.get("premise"),
        reason=meta.get("reason"),
    )


def _shape_practical_block(ans: dict[str, Any], grounded_ids: set[str]) -> PracticalBlockOut | None:
    """Shape the engine's ``practical`` block into a ``PracticalBlockOut`` with the citation check.

    Returns None when the engine omitted the block (it only produces one under its own
    structured_answer_enabled flag) or the value is not a dict.

    An item whose ``provision_id`` is set but not grounded is a forged claim: it is dropped from
    ``items`` and its ``slot`` appended to ``demoted_slots``, so the loss is visible. Items with a
    grounded ``provision_id`` are kept, as are items whose ``provision_id`` is None (they cite
    nothing, so they cannot forge a citation). ``gaps`` and the engine's own ``demoted_slots``
    pass through unchanged, since they are explicit non-claims rather than citations.
    """
    raw = ans.get("practical")
    if not isinstance(raw, dict):
        return None

    kept_items: list[PracticalItemOut] = []
    demoted_slots: list[str] = list(raw.get("demoted_slots") or [])
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        pid = it.get("provision_id")
        if pid is not None and str(pid) not in grounded_ids:
            # Forged or ungrounded practical claim: drop the item and demote its slot.
            slot = it.get("slot")
            if slot is not None and str(slot) not in demoted_slots:
                demoted_slots.append(str(slot))
            continue
        kept_items.append(
            PracticalItemOut(
                slot=str(it.get("slot") or ""),
                fact_type=str(it.get("fact_type") or ""),
                value_raw=it.get("value_raw"),
                provision_id=str(pid) if pid is not None else None,
                grounded=bool(it.get("grounded", True)),
            )
        )

    gaps = [g for g in (raw.get("gaps") or []) if isinstance(g, dict)]
    # B2: the "Co se na vás vztahuje" (`applies`) slot is deliberately not ranked by the profile's
    # `sme_roles` here; that belongs to the cascade lane (to revisit at plan step 96). Practical
    # items carry no role or actor signal (see PracticalItemOut) and the engine emits no per-item
    # role, so ranking here would need either a fragile free-text heuristic on value_raw or the
    # cascade's per-finding role determination. That determination already exists in the cascade:
    # run_applicability_scoring (analysis/applicability_scoring.py) resolves the company's role
    # (behind sme_roles_enabled) and stamps payload['applicability'] on every obligation finding,
    # demoting out-of-scope ones rather than deleting them. So the `applies` items keep the
    # engine's order, unannotated (pinned by tests/api/test_b2_role_ranking.py).
    return PracticalBlockOut(
        items=kept_items,
        gaps=gaps,
        demoted_slots=demoted_slots,
        profile_used=bool(raw.get("profile_used", False)),
    )


# Intake fact fields the understanding panel may show and offer for saving to the profile. Anything
# else the intake extracts is dropped (never shown, never suggested): a personal-data whitelist,
# even though the facts are the asker's own. The fields mirror the app's scoping vocabulary
# (AnswerContext and ProfilePatch), so a suggestion maps cleanly onto the profiles API. Values are
# passed as plain strings; no free-text field is ever admitted.
_INTAKE_FACT_WHITELIST: frozenset[str] = frozenset(
    {
        "legal_form",
        "nace_code",
        "headcount_band",
        "turnover_band",
        "jurisdiction",
        "municipality_code",
        "preferred_language",
        "processes_personal_data",
        "handles_client_money",
        "has_real_estate",
        "cross_border",
        "is_employer",
        "sector",
        "actor",
        "role",
        "obligation_type",
    }
)

# The whitelisted fields that map to a profile column and may be offered for saving: the five
# compliance flags and the core scoping fields. Question-specific fields (actor, role,
# obligation_type) describe this question rather than the business, so they are shown in the panel
# but not offered. `turnover_band` and `sector` are shown too but not offered, because ProfilePatch
# has no such columns and the PATCH would silently drop them.
_PROFILE_SUGGESTION_FIELDS: frozenset[str] = frozenset(
    {
        "legal_form",
        "nace_code",
        "headcount_band",
        "jurisdiction",
        "municipality_code",
        "preferred_language",
        "processes_personal_data",
        "handles_client_money",
        "has_real_estate",
        "cross_border",
        "is_employer",
    }
)

# An intake conflict is shaped to a whitelisted {field, values}. The engine emits
# `profile_conflicts` = [{field, profile_value, query_value}]: the profile's value and the
# question's conflicting value for the same field. Only those two value keys are read.
_CONFLICT_SCALAR_KEYS: tuple[str, ...] = ("profile_value", "query_value")


def _shape_intake(ans: dict[str, Any]) -> IntakeOut | None:
    """Shape the engine's ``audit_trace["intake"]`` into a whitelisted ``IntakeOut``.

    Returns None when the engine omitted it (the engine only attaches it under its own intake_v2
    flag) or the value is not a dict.

    Safe against unknown shapes. Only these are passed through: the ``sub_questions`` (strings),
    ``facts`` whose ``field`` is in ``_INTAKE_FACT_WHITELIST``, the ``conflicts`` on whitelisted
    fields, a ``sufficiency`` string and the ``dropped`` facts (field and reason). Every value is
    coerced to a string; no other free text is included.
    """
    raw = (
        (ans.get("audit_trace") or {}).get("intake")
        if isinstance(ans.get("audit_trace"), dict)
        else None
    )
    if not isinstance(raw, dict):
        return None

    sub_questions = [
        str(q)
        for q in (raw.get("sub_questions") or [])
        if isinstance(q, (str, int, float)) and str(q).strip()
    ]

    facts: list[IntakeFactOut] = []
    for f in raw.get("facts") or []:
        if not isinstance(f, dict):
            continue
        field = f.get("field")
        if not isinstance(field, str) or field not in _INTAKE_FACT_WHITELIST:
            continue  # not whitelisted or malformed: drop it (personal-data safeguard)
        val = f.get("value")
        if val is None:
            continue
        src = f.get("source")
        facts.append(
            IntakeFactOut(
                field=field,
                value=str(val),
                source=str(src) if src is not None else None,
            )
        )

    # The engine's key is `profile_conflicts`; each entry is {field, profile_value, query_value}.
    conflicts: list[IntakeConflictOut] = []
    for c in raw.get("profile_conflicts") or []:
        if not isinstance(c, dict):
            continue
        field = c.get("field")
        if not isinstance(field, str) or field not in _INTAKE_FACT_WHITELIST:
            continue  # conflict on a missing or non-whitelisted field: drop it
        vals: list[str] = []
        for k in _CONFLICT_SCALAR_KEYS:
            x = c.get(k)
            if isinstance(x, (str, int, float)) and str(x).strip():
                vals.append(str(x))
        seen_v: set[str] = set()
        vals = [v for v in vals if not (v in seen_v or seen_v.add(v))]  # de-duplicate, keep order
        if vals:
            conflicts.append(IntakeConflictOut(field=field, values=vals))

    sufficiency_raw = raw.get("sufficiency")
    sufficiency = str(sufficiency_raw) if isinstance(sufficiency_raw, (str, int, float)) else None

    # Facts dropped by the intake verifier (iv_verdicts). Each verdict is {item_id, verifier,
    # status, reason, evidence} with status one of pass, drop, conflict or advisory; a drop has
    # status 'drop'. The field shown is evidence['field'] when present, else the item_id (such as
    # 'fact:2', which contains no personal data); the reason is the stable code.
    dropped: list[IntakeDroppedOut] = []
    for v in raw.get("iv_verdicts") or []:
        if not isinstance(v, dict) or str(v.get("status")) != "drop":
            continue
        ev = v.get("evidence") if isinstance(v.get("evidence"), dict) else {}
        ev_field = ev.get("field")
        field = (
            ev_field
            if (isinstance(ev_field, str) and ev_field in _INTAKE_FACT_WHITELIST)
            else v.get("item_id")
        )
        if not isinstance(field, str) or not field.strip():
            continue
        dropped.append(
            IntakeDroppedOut(
                field=str(field),
                reason=(str(v.get("reason")) if v.get("reason") is not None else None),
            )
        )

    return IntakeOut(
        sub_questions=sub_questions,
        facts=facts,
        conflicts=conflicts,
        sufficiency=sufficiency,
        dropped=dropped,
    )


def _profile_suggestions_from_intake(intake: IntakeOut) -> list[ProfileSuggestionOut]:
    """Derive the profile save suggestions from the whitelisted intake facts.

    Only facts whose field is a profile column (``_PROFILE_SUGGESTION_FIELDS``) are offered,
    de-duplicated by field (the first wins, keeping intake order). Nothing is written here: the
    frontend saves a suggestion through the profiles API only on an explicit user action.
    """
    seen: set[str] = set()
    out: list[ProfileSuggestionOut] = []
    for f in intake.facts:
        if f.field not in _PROFILE_SUGGESTION_FIELDS or f.field in seen:
            continue
        seen.add(f.field)
        out.append(ProfileSuggestionOut(field=f.field, value=f.value))
    return out


def _strip_evidence_fulltext(ans: dict[str, Any]) -> None:
    """Blank, in place, any full provision text in the engine's evidence bundle.

    The engine already redacts this at its boundary; this is a second safeguard against an older
    engine that does not. Clears ``text`` and ``full_txt`` on each ``evidence.provisions`` row and
    leaves ids, labels and spans untouched. Tolerates any shape.
    """
    ev = ans.get("evidence")
    if not isinstance(ev, dict):
        return
    provs = ev.get("provisions")
    if not isinstance(provs, list):
        return
    for p in provs:
        if isinstance(p, dict):
            for k in ("text", "full_txt"):
                if k in p:
                    p[k] = ""


def _clarify_response_from_engine(ans: dict[str, Any], *, lang: str = "cs") -> AnswerResponse:
    """Shape the engine's clarify answer (render_mode="clarify") into the app's clarify frame.

    A clarify is not a refusal: refused=False, render_mode and band are "clarify", and it carries
    the information-not-advice disclaimer, the lawyer referral and the NEEDS_CLARIFICATION reason.
    It uses the same copy as the app-side clarify, so both look identical. The engine sends no
    slot questions (the slot form is app-side), so ``questions`` is empty; ``matter_type`` is
    taken from the engine trace when present. The caller returns this before the citation check,
    so a clarify is never downgraded to a refusal.
    """
    at = ans.get("audit_trace") if isinstance(ans, dict) else None
    matter = None
    if isinstance(at, dict):
        # The engine puts matter_type at the top of the trace; fall back to the intake fragment's
        # matter_type in case the top-level key is missing.
        matter = at.get("matter_type")
        if matter is None and isinstance(at.get("intake"), dict):
            matter = at["intake"].get("matter_type")
    disclaimer = route_copy.route_copy(route_copy.KEY_DISCLAIMER, lang)["disclaimer"]
    reason_enum = _RENDER_MODE_TO_REFUSAL_REASON.get(_RENDER_MODE_CLARIFY)
    return AnswerResponse(
        refused=False,
        render_mode=_RENDER_MODE_CLARIFY,
        band=_RENDER_MODE_CLARIFY,
        matter_type=str(matter) if matter is not None else None,
        title=_CLARIFY_TITLE.get(lang, _CLARIFY_TITLE["cs"]),
        body=_CLARIFY_BODY.get(lang, _CLARIFY_BODY["cs"]),
        disclaimer=disclaimer,
        reason=str(reason_enum) if reason_enum is not None else None,
    )


def _shape_answer(ans: dict[str, Any], *, lang: str = "cs") -> AnswerResponse:
    """Shape an engine answer dict into an AnswerResponse, enforcing the citation check.

    1. Resolve the grounded id set (cited_provisions plus grounded gate citations).
    2. Keep only grounded chips from the flat citations_map (drop forged citations).
    3. Resolve the render mode (engine to app; when the mode is missing or unknown, refuse if
       the engine refused, otherwise full).
    4. If the engine drafted prose but no chip is grounded, downgrade to a refusal: an uncited
       drafted claim is never emitted.
    5. Add the route_copy frame for a non-sound mode, or the hedge banner for a hedged one.
    6. Add the unknown-effective-date caveat when a cited provision has no valid_from.
    """
    grounded_ids = _cited_provision_ids(ans)
    drafted = ans.get("drafted_answer")
    engine_refused = bool(ans.get("refused"))

    # Grounded chips (flat list), checked one by one.
    flat_chips: list[ChipOut] = []
    for entry in ans.get("citations_map") or []:
        chip = _chip_from_map(entry, grounded_ids)
        if chip is not None:
            flat_chips.append(chip)

    # Sections, each with its own grounded chips. The citation check also applies per section: a
    # section that cited something but whose every citation is ungrounded is an uncited claim and
    # is dropped, even if a sibling section is clean. A section that cites nothing (an assessment
    # drawing on the framework already cited, or an empty case-law section) is kept: it forges no
    # citation, and the engine's verification pass still guarantees whole-answer grounding.
    sections: list[SectionOut] = []
    for sec in ans.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        sec_text = str(sec.get("text") or "")
        sec_raw_cites = sec.get("citations_map") or []
        sec_chips = [
            chip
            for entry in sec_raw_cites
            if (chip := _chip_from_map(entry, grounded_ids)) is not None
        ]
        if sec_text.strip() and sec_raw_cites and not sec_chips:
            # The section cited something, but every citation is ungrounded: drop it.
            continue
        sections.append(
            SectionOut(
                kind=str(sec.get("kind") or "assessment"),
                heading=str(sec.get("heading") or ""),
                text=sec_text,
                chips=sec_chips,
            )
        )

    # Fallback: when the engine drafted an answer with grounded citations but returned no parseable
    # sections (for example, the LLM used unexpected headings), render the whole draft as one
    # section so the prose is not silently lost. The flat check below still refuses if no citation
    # is grounded. This applies only when no sections were parsed at all, not when sections were
    # parsed and all dropped as forged above: re-surfacing the draft then would emit exactly the
    # uncited prose that was just removed (see
    # test_section_level_every_prose_section_forged_refuses).
    if (
        not sections
        and not ans.get("sections")
        and not engine_refused
        and drafted
        and drafted.strip()
        and flat_chips
    ):
        sections = [SectionOut(kind="legislative", heading="", text=str(drafted), chips=flat_chips)]

    # Render mode.
    engine_mode = ans.get("render_mode")
    app_mode = _ENGINE_TO_APP_RENDER_MODE.get(str(engine_mode)) if engine_mode else None
    if app_mode is None:
        # No recognised render mode (the engine's gate is off, or the value is unknown): a
        # grounded answer with prose is full, an engine refusal is refuse.
        app_mode = "refuse" if engine_refused else "full"

    # Shadow weak-coverage upgrade (answer_shadow_weak_coverage_hedged, off by default). The
    # engine's gate ran in shadow (advisory) mode and still drafted a grounded, self-hedged answer
    # for a "not in corpus" route whose coverage is only weak (related material, not an exact match,
    # as for AML or GDPR questions). Show that draft as a hedged answer instead of the closest-match
    # frame. Strictly scoped: only shadow mode with coverage 'weak_coverage', no engine refusal and
    # verdict 'ok'. No coverage (drafts would cite unrelated law), not-a-legal-question, false
    # premise and engine refusals are unaffected, and the citation check below still downgrades an
    # upgraded answer without grounded chips back to a refusal.
    if (
        app_mode == "closest"
        and not engine_refused
        and str(ans.get("verdict")) == "ok"
        and str((ans.get("meta") or {}).get("coverage")) == "weak_coverage"
        and str((ans.get("audit_trace") or {}).get("metagate_mode")) == "shadow"
        and get_settings().answer_shadow_weak_coverage_hedged
    ):
        app_mode = "hedged"

    # Engine clarify: return the clarify frame (see _clarify_response_from_engine) before the
    # citation check, so a clarify without chips is never turned into a refusal.
    if app_mode == _RENDER_MODE_CLARIFY:
        return _clarify_response_from_engine(ans, lang=lang)

    # No grounded support: on a sound path with drafted prose, downgrade to the refusal frame when
    # no chip survived the flat check, or when every prose section was dropped above as forged.
    if app_mode in ("full", "hedged") and drafted:
        had_sections = bool(ans.get("sections"))
        surviving_prose = any(s.text.strip() for s in sections)
        if not flat_chips or (had_sections and not surviving_prose):
            app_mode = "refuse"
            sections = []

    # Refused exactly when the mode is non-sound (notlegal, closest, falsepremise, refuse). Engine
    # refusals and the citation-check downgrade have both set app_mode to "refuse" by now, so this
    # one test covers every refused path; full and hedged are the only sound modes.
    refused = app_mode in _NON_SOUND_MODES

    resp = AnswerResponse(
        sections=sections if app_mode in ("full", "hedged") else [],
        chips=flat_chips if app_mode in ("full", "hedged", "closest", "falsepremise") else [],
        meta=_meta_out(ans),
        # A sound answer's band is its render mode, so a shadow weak-coverage upgrade reports
        # 'hedged' rather than the engine's advisory 'closest'. Non-sound frames keep the engine's
        # band, falling back to app_mode.
        band=app_mode if app_mode in ("full", "hedged") else (ans.get("band") or app_mode),
        render_mode=app_mode,
        refused=refused,
    )

    # Copy for a non-sound frame.
    if app_mode in _NON_SOUND_MODES:
        copy = route_copy.route_copy(_RENDER_MODE_TO_ROUTE[app_mode], lang)
        resp.title = copy["title"]
        resp.body = copy["body"]
        resp.disclaimer = copy["disclaimer"]
        reason_enum = _RENDER_MODE_TO_REFUSAL_REASON.get(app_mode)
        resp.reason = str(reason_enum) if reason_enum is not None else (ans.get("reason") or None)
    else:
        # A sound answer carries the standard information-not-advice disclaimer.
        resp.disclaimer = route_copy.route_copy(route_copy.KEY_DISCLAIMER, lang)["disclaimer"]

    # Hedge banner for a sound but hedged answer.
    if app_mode == "hedged":
        resp.hedge_banner = route_copy.route_copy(route_copy.KEY_HEDGE_BANNER, lang)["body"]

    # Unknown-effective-date caveat ("datum účinnosti neuvedeno").
    if not refused and _has_null_valid_from(ans):
        resp.as_of_unknown_note = route_copy.route_copy(
            route_copy.KEY_DISCLAIMER_AS_OF_UNKNOWN, lang
        )["body"]

    # Structured practical block (answer_practical_block_enabled): attached only to a sound answer,
    # since a non-answer makes no practical claims. Items are checked against the same grounded id
    # set as the chips (see _shape_practical_block).
    if not refused and get_settings().answer_practical_block_enabled:
        resp.practical = _shape_practical_block(ans, grounded_ids)

    # Understanding panel (answer_intake_panel_enabled): attached to every frame, because showing
    # which facts were extracted helps even when the question was not answered. It is whitelisted,
    # informational data. The profile suggestions derive from the whitelisted intake facts and are
    # never saved automatically.
    if get_settings().answer_intake_panel_enabled:
        intake = _shape_intake(ans)
        if intake is not None:
            resp.intake = intake
            resp.profile_suggestions = _profile_suggestions_from_intake(intake)

    return resp


# ---------------------------------------------------------------------------
# The POST handler
# ---------------------------------------------------------------------------
# Entitlement feature name for the answer endpoint. The guard is called inline in the handler, not
# as a FastAPI dependency, so it does not pull in `get_principal` as a sub-dependency:
# `get_principal`'s `Request | None` signature cannot be resolved by FastAPI's Depends.
_ANSWER_FEATURE = "answer"


@router.post("", response_model=AnswerResponse)
async def ask_answer(
    req: AskAnswerRequest,
    request: Request,
) -> AnswerResponse:
    """Answer one self-contained legal question with a grounded, cited response.

    The response is either a drafted answer (sections with citation chips, `refused: false`) or a
    non-answer frame (`refused: true`) explaining why there is no answer: not a legal question,
    not covered by the corpus, a false premise, or rejected by verification. Every chip cites a
    provision the engine verified. With `render_mode: "clarify"` the response asks for missing
    facts instead; resubmit the question with the answers in `slots` (at most one round).

    Errors: 404 when the endpoint is disabled, 422 for unknown body fields or invalid `slots`,
    402 or 403 when the plan does not include answers, 429 when the daily query quota is used
    up, 502 for a malformed engine response, and 503 while spending is paused (spend cap or kill
    switch).
    """
    # Processing order: principal and entitlement; input compliance gate; app-side clarify (no
    # spend, before quota); profile context; quota reservation (429 before any spend); spend
    # precheck; engine call; response shaping with the citation check; output compliance gate.
    _require_enabled()

    principal: Principal = await get_principal(request)
    org_id = principal.org_id

    # Entitlement check, before any spend. With entitlements_enabled=False the guard returns
    # without resolving anything; when enabled it raises 402 or 403 here.
    _guard = require_entitlement(_ANSWER_FEATURE)
    async with session_scope() as _gsession:
        await _guard(principal=principal, session=_gsession)

    # Input compliance gate. In shadow mode (the default) it only classifies and logs.
    settings = get_settings()
    if getattr(settings, "compliance_guard_enabled", False):
        _verdict = await classify_answer_mode(text=req.query, where="input", org_id=org_id)
        if (
            getattr(settings, "compliance_guard_mode", "shadow") == "enforce"
            and _verdict.answer_mode == "reserved_advice"
        ):
            # Enforce mode (reserved for after legal sign-off): a request for reserved advice
            # gets the refusal frame, with no engine call and no spend.
            copy = route_copy.route_copy(route_copy.ROUTE_REFUSE, req.lang)
            return AnswerResponse(
                refused=True,
                render_mode="refuse",
                band="refuse",
                reason=str(RefusalReason.RESERVED_ADVICE),
                title=copy["title"],
                body=copy["body"],
                disclaimer=copy["disclaimer"],
            )

    # App-side clarify (answer_clarify_enabled, off by default): deterministic, no LLM, no spend.
    # It runs after the input gate, so an enforced reserved-advice refusal still wins, and before
    # the quota reservation, so a clarify reserves nothing and never calls the engine.
    #
    # It fires only on the first turn (req.slots is None) when the matter type detected from the
    # question has a required slot that typed slots, profile and question leave unresolved. At
    # most one round: a follow-up with `slots` is validated and goes straight on to quota and
    # engine, even if still incomplete (the engine then answers hedged).
    #
    # _slot_context holds the facts resolved from a follow-up's slots; it is merged into
    # project_context below so they reach the engine's intake.
    _slot_context: dict[str, Any] = {}
    _slot_missing: list[str] = []
    if getattr(settings, "answer_clarify_enabled", False):
        if req.slots is not None:
            # Follow-up turn: validate the resubmitted slots against the slot enums (unknown
            # top-level fields were already rejected by extra='forbid').
            _err = clarify_mod.validate_slots(req.slots)
            if _err is not None:
                raise HTTPException(status_code=422, detail=f"invalid slots: {_err}")
            # Resolve the slots (typed slots first, then profile, then question) into known facts
            # for the engine's intake. They only add context; a profile value for the same field
            # wins in the merge below. An incomplete follow-up is never asked to clarify again;
            # the engine answers hedged over the gaps.
            _fu_ctx = await _load_answer_context(req.project_id, org_id) if req.project_id else None
            _fu_res = clarify_mod.resolve_slots(
                req.query, answer_context=_fu_ctx, slots_provided=req.slots
            )
            _slot_context = dict(_fu_res.resolved)
            _slot_missing = list(_fu_res.missing_required)
        else:
            # First turn: may return a clarify frame. Slots are resolved from the profile (None if
            # unavailable, in which case only the question is read) and a deterministic read of
            # the question. No quota has been reserved yet.
            _clarify_ctx = (
                await _load_answer_context(req.project_id, org_id) if req.project_id else None
            )
            _resolution = clarify_mod.resolve_slots(
                req.query, answer_context=_clarify_ctx, slots_provided=None
            )
            if _resolution.needs_clarify:
                _questions = clarify_mod.clarify_questions(
                    _resolution.missing_required, lang=req.lang
                )
                # A clarify carries the information-not-advice disclaimer and the lawyer
                # referral. It is not a refusal (refused=False); the frontend renders it as a form.
                _disclaimer = route_copy.route_copy(route_copy.KEY_DISCLAIMER, req.lang)[
                    "disclaimer"
                ]
                _referral = _CLARIFY_REFERRAL.get(req.lang, _CLARIFY_REFERRAL["cs"])
                _log.info(
                    "answer_clarify_returned",
                    org_id=str(org_id),
                    matter_type=_resolution.matter_type,
                    missing=_resolution.missing_required,
                )
                # Control-room intake telemetry (counts only, no question text).
                try:
                    await events.emit(
                        "intake.clarify.returned",
                        module="polarius",
                        org_id=str(org_id),
                        matter_type=str(_resolution.matter_type),
                        n_missing=len(_resolution.missing_required),
                    )
                except Exception:  # telemetry must never break the answer path
                    _log.warning("intake_clarify_event_failed")
                return AnswerResponse(
                    refused=False,
                    render_mode=_RENDER_MODE_CLARIFY,
                    band=_RENDER_MODE_CLARIFY,
                    matter_type=_resolution.matter_type,
                    questions=[ClarifyQuestionOut(slot=q.slot, text=q.text) for q in _questions],
                    title=_CLARIFY_TITLE.get(req.lang, _CLARIFY_TITLE["cs"]),
                    body=_CLARIFY_BODY.get(req.lang, _CLARIFY_BODY["cs"]),
                    disclaimer=_disclaimer,
                    reason=_referral,
                )

    # Profile context (answer_profile_context_enabled, off by default): seed the engine's intake
    # with known facts from the project's business profile, as the small whitelisted dict built by
    # _serialize_answer_context. This read runs before the quota reservation, so the reservation
    # stays the last gate before spend (pinned by a test). Without the flag or a project_id,
    # project_context stays None. The dict goes to the engine for intake only and is never stored
    # in api_call.request_json, so cost rows hold no profile data.
    project_context: dict[str, Any] | None = None
    if settings.answer_profile_context_enabled and req.project_id:
        project_context = await _load_profile_context(req.project_id, org_id)

    # Quota before spend: the reservation happens before the engine call, so an over-quota
    # request fails with 429 having spent nothing. With entitlements disabled,
    # metering.resolve_for_entry returns None and nothing is reserved.
    reserved_metrics: list[str] = []
    ent = await metering.resolve_for_entry(org_id)
    if ent is not None:
        async with session_scope() as _qsession:
            # An answer is one `queries` unit, reserved atomically.
            from regsuper.observability import limits as _limits

            await _limits.check_and_reserve_quota(
                _qsession, org_id, _METRIC_QUERIES, limit=ent.queries_per_day
            )
            reserved_metrics.append(_METRIC_QUERIES)

    # Merge the follow-up's slot facts into the known-facts context, independently of the profile
    # flag, so slots reach the engine even without a profile. A profile value wins over a slot for
    # the same field; slots fill in the rest.
    if _slot_context:
        project_context = {**_slot_context, **(project_context or {})}

    # Record that known facts were sent to the engine (count only, no content).
    if project_context:
        try:
            await events.emit(
                "intake.context.attached",
                module="polarius",
                org_id=str(org_id),
                n_facts=len(project_context),
            )
        except Exception:  # telemetry must never break the answer path
            _log.warning("intake_context_event_failed")

    # Spend precheck (answer_budget_precheck_enabled, off by default), immediately before the
    # engine call and after the quota reservation (which has already raised 429 if over quota).
    # It checks the kill switch and the spend caps of the engine LLM provider, adding the
    # worst-case cost of one answer to the day's spend. On a breach it returns a structured refusal
    # frame (not an error) without calling the engine, and refunds the reserved quota unit. This
    # backs up the spend gate in the engine client, whose failure would surface as a 503.
    if getattr(settings, "answer_budget_precheck_enabled", False):
        try:
            assert_not_killed()
            await enforce_spend_limit(
                provider=_PROVIDER_ENGINE_LLM,
                projected_cost_usd=_ANSWER_ENGINE_WORST_CASE_COST_USD,
            )
        except (SpendLimitExceeded, KillSwitchEngaged) as _gexc:
            # Refund the reserved unit: a request that never reached the engine consumes no quota.
            # Clearing reserved_metrics makes a second refund of this unit impossible.
            if reserved_metrics:
                try:
                    async with session_scope() as _psession:
                        await metering.refund_for_run(
                            _psession, org_id=org_id, reserved_metrics=reserved_metrics
                        )
                except Exception:  # a refund failure must never mask the refusal
                    _log.warning("answer_precheck_quota_refund_failed", org_id=str(org_id))
                reserved_metrics.clear()  # a second refund of this unit is now impossible
            _log.info("answer_budget_precheck_refused", org_id=str(org_id), reason=str(_gexc))
            copy = route_copy.route_copy(route_copy.ROUTE_REFUSE, req.lang)
            return AnswerResponse(
                refused=True,
                render_mode="refuse",
                band="refuse",
                reason=str(RefusalReason.NO_COVERAGE),
                title=copy["title"],
                body=copy["body"],
                disclaimer=copy["disclaimer"],
            )

    # The engine call (a long-running HTTP request to the Pythia engine).
    try:
        ans = await pythia.answer(
            req.query,
            as_of=req.as_of.isoformat() if req.as_of else None,
            project_context=project_context,
            org_id=org_id,
            analysis_run_id=None,
        )
    except Exception as _exc:
        # No billable answer was produced: refund the reserved unit so a transport failure does
        # not consume the user's quota, then report the error.
        if reserved_metrics:
            try:
                async with session_scope() as _rsession:
                    await metering.refund_for_run(
                        _rsession, org_id=org_id, reserved_metrics=reserved_metrics
                    )
            except Exception:  # a refund failure must not mask the original error
                _log.warning("answer_quota_refund_failed", org_id=str(org_id))
        # The engine client's spend gate (pythia_boundary_spend_gate_enabled) raises before any
        # spend when a spend cap is reached or the kill switch is engaged. Map both to a 503
        # "temporarily paused" instead of a 500; the quota unit was refunded above. Any other
        # failure is re-raised.
        if isinstance(_exc, (SpendLimitExceeded, KillSwitchEngaged)):
            raise HTTPException(
                status_code=503,
                detail="answer service temporarily paused (spend cap or kill switch)",
            ) from _exc
        raise

    if not isinstance(ans, dict):
        raise HTTPException(status_code=502, detail="malformed answer from the reasoning engine")

    # Refund after an engine clarify: the engine returns render_mode="clarify" before drafting, so
    # nothing billable was produced and the reserved `queries` unit is refunded, just as the
    # app-side clarify reserves nothing. This cannot double up with the transport-failure refund
    # above (that path raised, this one returned), and clearing reserved_metrics makes a second
    # refund impossible anyway.
    if reserved_metrics and str(ans.get("render_mode")) == "clarify":
        try:
            async with session_scope() as _csession:
                await metering.refund_for_run(
                    _csession, org_id=org_id, reserved_metrics=reserved_metrics
                )
        except Exception:  # a refund failure must never break the clarify answer
            _log.warning("answer_clarify_quota_refund_failed", org_id=str(org_id))
        reserved_metrics.clear()  # makes a second refund of this unit impossible

    # The engine already redacts full provision text from its evidence; strip it here as well so
    # an older engine can never leak it further.
    _strip_evidence_fulltext(ans)

    # Shape the response and apply the citation check.
    resp = _shape_answer(ans, lang=req.lang)

    # Assumptions: when a follow-up still left required slots empty, the engine answered over the
    # gaps; list those slots so the reader sees what was assumed. Not on a refusal (nothing was
    # answered) nor on a clarify frame (which is itself the question).
    if _slot_missing and not resp.refused and resp.render_mode != _RENDER_MODE_CLARIFY:
        resp.assumptions = list(_slot_missing)

    # Competent offices (authority_registry_enabled, off by default): resolved from the derived
    # topic and the asker's municipality (profile first, then the intake fact), and attached only
    # to a sound answer. The resolver never guesses: an unresolved topic or municipality leaves the
    # list empty. Only the whitelisted AuthorityOut fields are returned, and nothing is persisted.
    if getattr(get_settings(), "authority_registry_enabled", False) and not resp.refused:
        _auth_ctx = await _load_answer_context(req.project_id, org_id) if req.project_id else None
        resp.authorities = _resolve_authorities(ans, _auth_ctx)

    # Output compliance gate on the drafted prose (classifies and logs; never blocks).
    if getattr(settings, "compliance_guard_enabled", False) and not resp.refused:
        _drafted = ans.get("drafted_answer") or ""
        if _drafted:
            await classify_answer_mode(text=_drafted, where="output", org_id=org_id)

    # Gap telemetry (best effort): one event per gap in the practical block, so gap frequencies
    # can drive the backlog of data sources to add. Events only, no new data store.
    if resp.practical is not None:
        await _emit_gap_telemetry(resp.practical, org_id)

    # Claim-verification summary from the engine (only when its verification loop ran).
    await _emit_claims_summary(ans, org_id)

    # Claim-ledger export to disk as canonical JSON (claim_ledger_export_enabled, off by default):
    # fire-and-forget, so it never delays or blocks the answer, and a no-op without
    # `audit_trace["claim_ledger"]`. The question text is passed so the exporter can apply its
    # redaction rule: it is written only when conduit_capture_query_text is True. Failures are
    # logged and swallowed inside schedule_ledger_export.
    _ledger_export.schedule_ledger_export(ans, query_text=req.query)

    # Intake conflict and dropped-fact telemetry (counts only; only when the panel is present).
    if resp.intake is not None:
        try:
            if resp.intake.conflicts:
                await events.emit(
                    "intake.conflict",
                    module="polarius",
                    org_id=str(org_id),
                    n_conflicts=len(resp.intake.conflicts),
                )
            if resp.intake.dropped:
                await events.emit(
                    "intake.facts.dropped",
                    module="polarius",
                    org_id=str(org_id),
                    n_dropped=len(resp.intake.dropped),
                )
        except Exception:  # telemetry must never break the answer path
            _log.warning("intake_panel_event_failed")

    return resp


async def _emit_claims_summary(ans: dict[str, Any], org_id: Any) -> None:
    """Emit one ``answer.claims.summary`` event from the engine's claim-ledger summary.

    The ledger (``audit_trace['claim_ledger']``) is present only when the engine's
    claim-verification loop ran; otherwise nothing is emitted. Counts only: no question, claim or
    provision text enters the payload. Best effort: a telemetry failure never breaks the answer.
    """
    at = ans.get("audit_trace") if isinstance(ans, dict) else None
    ledger = (at or {}).get("claim_ledger") if isinstance(at, dict) else None
    summary = (ledger or {}).get("summary") if isinstance(ledger, dict) else None
    if not isinstance(summary, dict):
        return
    per_status = summary.get("per_status") or {}
    dropped = sum(int(v or 0) for k, v in per_status.items() if str(k).startswith("dropped"))
    try:
        await events.emit(
            "answer.claims.summary",
            module="pythia",
            org_id=str(org_id),
            mode=str(summary.get("mode") or "shadow"),
            claims_total=int(summary.get("claims_total", 0) or 0),
            demoted=int(summary.get("demoted", 0) or 0),
            dropped=int(dropped),
            claim_verify_ms=summary.get("claim_verify_ms"),
        )
    except Exception:  # telemetry must never break the answer path
        _log.warning("answer_claims_summary_emit_failed")


# Maps the practical block's `gap_class` values to the canonical eight-code gap_kind backlog
# vocabulary (events.GAP_KIND_BACKLOG_CODES, kept identical to the engine's). Classes without a
# backlog equivalent fall back to `soft_law_absent`, the benign catch-all, so the rollup only counts
# canonical codes. Gaps that already carry a canonical `gap_kind` (from the engine's demotion path)
# use it unchanged.
_GAP_CLASS_TO_KIND: dict[str, str] = {
    "authority_locator": "authority_locator",
    "form_portal": "form_portal",
    "soft_law_guidance": "soft_law_absent",
    "regional": "soft_law_absent",
    "case_law": "soft_law_absent",
    "situation_uncited_legal_claim": "no_text_support",
}


def _gap_kind_of(gap: dict[str, Any]) -> str:
    """Return the canonical gap_kind for one practical-block gap.

    Uses an explicit engine `gap_kind` if canonical, otherwise maps `gap_class`, otherwise
    `soft_law_absent`. Pure; never raises.
    """
    explicit = str(gap.get("gap_kind") or "")
    if explicit in events.GAP_KIND_BACKLOG_CODES:
        return explicit
    return _GAP_CLASS_TO_KIND.get(str(gap.get("gap_class") or ""), "soft_law_absent")


async def _emit_gap_telemetry(block: PracticalBlockOut, org_id: Any) -> None:
    """Emit telemetry for the gaps in a practical block, feeding the data-source backlog.

    Emits one ``answer.practical.gap`` event per gap, then one ``practical.gap`` event with the
    gap count and one ``practical.gap_backlog`` event with the most frequent canonical gap_kind and
    its count. Payloads hold only taxonomy labels and counts, never question, claim or provision
    text. Best effort: a telemetry failure never breaks the answer. Nothing is emitted when the
    block has no gaps.
    """
    for gap in block.gaps:
        gap_class = str(gap.get("gap_class") or gap.get("status") or "unknown")
        try:
            await events.emit(
                "answer.practical.gap",
                module="polarius",
                org_id=str(org_id),
                gap_class=gap_class,
            )
        except Exception:  # telemetry must never break the answer path
            _log.warning("answer_gap_telemetry_failed", gap_class=gap_class)

    # Counts-only rollup: the gap count and the top backlog code.
    n_gaps = len(block.gaps)
    if n_gaps == 0:
        return
    try:
        backlog = _rollup_gap_backlog(_gap_kind_of(g) for g in block.gaps)
        await events.emit("practical.gap", module="polarius", org_id=str(org_id), n_gaps=n_gaps)
        if backlog:
            top_kind, top_count = backlog[0]
            await events.emit(
                "practical.gap_backlog",
                module="polarius",
                org_id=str(org_id),
                n_gaps=n_gaps,
                top_gap_kind=top_kind,
                top_count=top_count,
            )
    except Exception:  # telemetry must never break the answer path
        _log.warning("practical_gap_backlog_emit_failed")


def _rollup_gap_backlog(gap_kinds: Any) -> list[tuple[str, int]]:
    """Count canonical gap_kind codes and rank them as ``[(code, count)]``.

    Ordered by count (descending), then by the code's position in
    events.GAP_KIND_BACKLOG_CODES, matching the engine's ranking. Non-canonical codes are
    dropped. Pure; never raises.
    """
    from collections import Counter

    order = {code: i for i, code in enumerate(events.GAP_KIND_BACKLOG_CODES)}
    counts: Counter = Counter(c for c in gap_kinds if c in order)
    return [(c, n) for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]]))]
