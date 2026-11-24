"""Deterministic intake pre-pass: classify the raw query with a reviewable lexicon, no LLM.

Applies the lexicon in pythia/data/intake_lexicon.json to the raw query and returns IntakeFacts with
``source='det'``. It complements the LLM extraction in ``intake.run_intake``: the LLM extracts facts
quoted from the query (each checked by IV-SPAN), while this pass matches the query against a closed
set of surface forms (legal-form aliases, gate cues, regime aliases, a small municipality gazetteer)
and emits the corresponding fact.

  * Pure and total: ``_det_scan(query) -> list[IntakeFact]``.
  * Accent-insensitive: the query and every surface form are NFKD-folded (combining marks removed)
    and lowercased, so ASCII forms match Czech with diacritics.
  * A det fact is a classification, not a quoted span, so it skips the IV-SPAN check (the quote must
    be a verbatim substring of the query) but still passes IV-ENUM (``field`` must be in
    ``intake._FACT_FIELDS``). ``det_scan_facts`` runs that check itself, so a lexicon rule with an
    unknown field can never leak a fact.
  * At most one fact per field (the first rule to claim it wins), so a query naming two legal forms
    yields one fact, not a contradiction. When merged into the record (intake.py), a det fact never
    overrides an LLM or profile fact for the same field.
  * A missing, unreadable or malformed lexicon yields ``[]``. The lexicon is read at most once per
    process.

Like other intake facts, det facts feed only the answer path and the audit trace, never the cascade,
candidate or verdict machinery (which is lawyer-gated). The pass runs only when
``config.intake_det_scan_enabled`` is on (off by default). The lexicon content is a draft pending
lawyer review.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pythia.reasoning.intake import _FACT_FIELDS, IntakeFact

# The lexicon ships inside the package at pythia/data/intake_lexicon.json (next to reasoning/). It
# is resolved relative to this package so it is found from any cwd; config.data_dir points at an
# external data tree.
_LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "intake_lexicon.json"

_PER_FORM = "__per_form__"  # sentinel: the value comes from the rule's forms_map, per matched form


def _fold_accents(s: str) -> str:
    """Fold accents and lowercase, so ASCII lexicon forms match Czech with diacritics.

    For example 'v Plzni' becomes 'v plzni' and 'osvč' becomes 'osvc'.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


@dataclass(frozen=True)
class _CompiledRule:
    """One compiled lexicon rule.

    ``field`` is checked against the vocabulary at load time and a rule with an unknown field is
    dropped, so ``_det_scan`` can never emit an out-of-vocabulary fact. If ``value`` is the
    ``_PER_FORM`` sentinel, the emitted value comes from ``form_values`` (one value per surface
    form); otherwise ``value`` is emitted for any matching form.
    """

    rule_id: str
    field: str
    value: str
    patterns: tuple[re.Pattern, ...]  # used when value != _PER_FORM
    form_values: tuple[tuple[re.Pattern, str], ...]  # used when value == _PER_FORM (regex, value)


def _compile_rules(raw: dict) -> list[_CompiledRule]:
    """Compile the parsed lexicon into rules.

    A malformed rule (missing or unknown field, no usable forms, invalid regex) is skipped rather
    than raising, so a partly broken lexicon still yields its valid rules instead of crashing when
    the feature is enabled.
    """
    rules: list[_CompiledRule] = []
    for r in raw.get("rules", []) or []:
        if not isinstance(r, dict):
            continue
        fld = r.get("field")
        # IV-ENUM at load time: only a field in the closed vocabulary may produce a det fact.
        if fld not in _FACT_FIELDS:
            continue
        rule_id = str(r.get("id", "") or "")
        value = r.get("value")
        try:
            if value == _PER_FORM:
                fm = r.get("forms_map")
                if not isinstance(fm, dict) or not fm:
                    continue
                form_values = tuple(
                    (re.compile(_fold_accents(str(pat))), str(val))
                    for pat, val in fm.items()
                    if str(pat).strip() and str(val).strip()
                )
                if not form_values:
                    continue
                rules.append(_CompiledRule(rule_id, fld, _PER_FORM, (), form_values))
            else:
                forms = r.get("forms")
                if not isinstance(forms, list) or not forms:
                    continue
                pats = tuple(re.compile(_fold_accents(str(f))) for f in forms if str(f).strip())
                if not pats or not isinstance(value, str) or not value.strip():
                    continue
                rules.append(_CompiledRule(rule_id, fld, value.strip(), pats, ()))
        except re.error:
            continue  # an invalid surface form drops just that rule
    return rules


# Cached compiled lexicon. `_load_rules()` reads the file at most once per process; the sentinel
# tells "not loaded yet" apart from "loaded but empty", so an empty or missing lexicon is not
# re-read.
_UNSET: object = object()
_RULES_CACHE: object = _UNSET


def _load_rules() -> list[_CompiledRule]:
    """Load and compile the lexicon once per process.

    A missing, unreadable or non-JSON lexicon yields (and caches) ``[]``, so enabling the feature
    never crashes on a bad or missing file.
    """
    global _RULES_CACHE
    if _RULES_CACHE is not _UNSET:
        return _RULES_CACHE  # type: ignore[return-value]
    try:
        raw = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
        rules = _compile_rules(raw) if isinstance(raw, dict) else []
    except (OSError, ValueError):
        rules = []
    _RULES_CACHE = rules
    return rules


def _reset_cache_for_tests() -> None:
    """Clear the cache so a test can point ``_LEXICON_PATH`` elsewhere and reload. Test use only."""
    global _RULES_CACHE
    _RULES_CACHE = _UNSET


def _det_scan(query: str) -> list[IntakeFact]:
    """Apply the lexicon to ``query`` and return IntakeFacts with ``source='det'``.

    At most one fact per field; the first rule to claim a field wins. Every field is in
    ``_FACT_FIELDS`` (enforced when the lexicon loads). A det fact has no query span (its
    ``evidence_quote`` records which rule fired), so it is exempt from IV-SPAN. An empty query or
    lexicon yields ``[]``.
    """
    folded = _fold_accents(query)
    if not folded.strip():
        return []
    facts: list[IntakeFact] = []
    claimed: set[str] = set()  # fields already emitted; later rules never override them
    for rule in _load_rules():
        if rule.field in claimed:
            continue
        if rule.value == _PER_FORM:
            matched_value: str | None = None
            for pat, val in rule.form_values:
                if pat.search(folded):
                    matched_value = val
                    break
            if matched_value is None:
                continue
            value = matched_value
        else:
            if not any(p.search(folded) for p in rule.patterns):
                continue
            value = rule.value
        claimed.add(rule.field)
        # evidence_quote records the rule id as provenance. A det fact is a classification, not a
        # query span, so this is never checked against the query (IV-SPAN is skipped for
        # source='det').
        facts.append(IntakeFact(field=rule.field, value=value, evidence_quote=f"det:{rule.rule_id}", source="det"))
    return facts


def det_scan_facts(query: str) -> list[IntakeFact]:
    """Run the deterministic pre-pass and validate its facts; the entry point the intake merge calls.

    Facts go through ``intake_verify.validate_det_facts``, the same registry that checks LLM facts:
    IV-SPAN is skipped (there is no verbatim span), but IV-ENUM still drops any fact with an unknown
    field. intake_verify is imported lazily; if it is unavailable, the load-time vocabulary check
    already guarantees safe fields, so the ``_det_scan`` output is returned unchanged.
    """
    facts = _det_scan(query)
    if not facts:
        return facts
    try:
        from pythia.reasoning import intake_verify as _iv
    except Exception:  # pragma: no cover - defensive; the load-time check already ensured safe fields
        return facts
    kept, _verdicts = _iv.validate_det_facts(facts)
    return kept
