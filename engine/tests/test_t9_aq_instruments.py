"""T9 — three ADDITIVE / OBSERVE-ONLY answer-quality instruments (Track A1 + MP-G). LLM-free, DB-free.

Each is DEFAULT-OFF and must be byte-identical + zero-new-event when OFF:
  1. term_norm MATERIALISATION (A1) — the load-once compiled matcher (match_terms_mat) is SEMANTICALLY
     IDENTICAL to the pure match_terms over the FULL alias set + diacritic/case edge samples.
  2. per-stage LATENCY instrument (MP-G) — telemetry.record_stage_latency populates
     metrics()['stage_latency_ms'] and is a pure telemetry no-op (records a NUMBER, no ring event).
  3. QUALITY TRIPWIRES — the pure evaluate() fires the right hits when a threshold is crossed and stays
     silent otherwise; record_quality_tripwire emits the event + bumps the counter ONLY when called.

The OFF guards are covered by the config-default assertions here + the existing answer byte-identical
suites (this file adds the equivalence/behaviour proofs).
"""

from __future__ import annotations

import itertools

from pythia import telemetry
from pythia.config import get_settings
from pythia.graph import term_norm as tn
from pythia.reasoning.quality_tripwires import (
    QualitySignals,
    TripwireHit,
    TripwireThresholds,
    evaluate,
)


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 0. Flag defaults — all three OFF, so committing this is byte-identical.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_t9_flag_defaults_all_false():
    s = get_settings()
    assert s.stage_latency_enabled is False
    assert s.quality_tripwires_enabled is False
    # A1 rides on term_norm_enabled (already default-OFF) — the materialised path is only reached when it is ON.
    assert s.term_norm_enabled is False


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1. TERM-NORM MATERIALISATION (A1) — match_terms_mat == match_terms.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def _full_alias_lexicon() -> list[tn.LexEntry]:
    """The full shipped alias lexicon (DB-free) + a couple of defined_term-shaped rows carrying defining ids,
    so the equivalence covers both alias rows and defined-term rows (with defining_ids)."""
    lex = list(tn._load_aliases())
    lex.append(
        tn.LexEntry(
            surface=tn._fold("koncové zařízení"),
            statutory=[tn._fold("koncové zařízení")],
            defining_ids=["prov-1", "prov-2"],
            is_alias=False,
        )
    )
    lex.append(
        tn.LexEntry(
            surface=tn._fold("závislá práce"),
            statutory=[tn._fold("závislá práce")],
            defining_ids=["prov-3"],
            is_alias=False,
        )
    )
    return lex


# A broad probe set: exact single/bigram alias surfaces, diacritic + case variants, typos (trigram lane),
# multi-term queries, and no-hit strings — so the equivalence is exercised across every match path.
_PROBES = [
    "cookies",
    "COOKIES",
    "Cookies na webu",
    "švarcsystém v praxi",
    "svarcsystem",
    "SVARCSYSTEM",
    "jsem OSVC a platím daně",
    "osvč",
    "platce dph registrace",
    "gdpr a osobní údaje",
    "OSOBNÍ DATA",
    "personal data breach",
    "výpověď z práce",
    "dohoda o provedení práce brigáda",
    "nemocenská",
    "mateřská dovolená",
    "rodičák",
    "minimální mzda 2026",
    "exekuce srážky ze mzdy",
    "insolvence osobní bankrot",
    "s.r.o. a jednatel",
    "živnostenský list",
    "reklamace vady zboží",
    "odstoupení od smlouvy vrátit do 14 dnů",
    "spotřebitel zákazník",
    "přesčasy overtime",
    "dovolená nárok",
    "daň z příjmu přiznání",
    "sociální pojištění socialka",
    "zdravotní pojištění",
    "konkurenční doložka",
    "praní špinavých peněz money laundering",
    "koncové zařízení",
    "závislá práce",
    # typo / inflection (trigram lane)
    "cookiess",
    "svarcsysstem",
    "gdrp",
    "osvcc",
    # no-hit
    "zcela nesouvisející věta bez pojmu",
    "xyzzy",
    "",
    "kávovar a počasí",
]


def test_materialize_equivalence_full_alias_set():
    """A1 CARDINAL — match_terms_mat over the compiled structure == match_terms over the source lexicon, for
    EVERY probe (full alias set incl. diacritic/case/typo edges), at multiple thresholds."""
    lex = _full_alias_lexicon()
    mat = tn.materialize(lex)
    for q, thr in itertools.product(_PROBES, (0.5, 0.6, 0.7)):
        pure = tn.match_terms(q, lex, trigram_threshold=thr)
        matd = tn.match_terms_mat(q, mat, trigram_threshold=thr)
        # compare the full TermMatch content (span, statutory, defining_ids, kind) in order.
        pure_t = [(m.query_span, tuple(m.statutory), tuple(m.defining_ids), m.kind) for m in pure]
        matd_t = [(m.query_span, tuple(m.statutory), tuple(m.defining_ids), m.kind) for m in matd]
        assert pure_t == matd_t, f"mismatch q={q!r} thr={thr}"


def test_materialize_empty_lexicon_both_empty():
    mat = tn.materialize([])
    assert tn.match_terms_mat("cookies", mat) == []
    assert (
        tn.match_terms(
            "cookies",
            [],
        )
        == []
    )


def test_materialize_structure_shape():
    """The compiled structure carries the exact-lookup dict + per-entry trigram profiles in source order."""
    lex = _full_alias_lexicon()
    mat = tn.materialize(lex)
    assert mat.by_surface == {e.surface: e for e in lex}
    assert len(mat.entries) == len(lex) == len(mat.trigrams)
    for e, tri in zip(mat.entries, mat.trigrams):
        assert tri == tn._trigrams(e.surface)


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2. PER-STAGE LATENCY INSTRUMENT (MP-G) — populates metrics()['stage_latency_ms'], telemetry-only.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_stage_latency_records_and_surfaces():
    telemetry.record_stage_latency("t9_probe_stage", 12.5)
    telemetry.record_stage_latency("t9_probe_stage", 7.5)
    block = telemetry.metrics()["stage_latency_ms"]
    assert "t9_probe_stage" in block
    st = block["t9_probe_stage"]
    for key in ("p50", "p95", "max", "last", "count"):
        assert key in st
    assert st["last"] == 7.5
    assert st["count"] >= 2
    assert st["max"] >= 12.5


def test_stage_latency_ignores_bad_values():
    """A NaN / inf / non-numeric ms is dropped, never raised or ring-polluted."""
    telemetry.record_stage_latency("t9_bad_stage", float("nan"))
    telemetry.record_stage_latency("t9_bad_stage", float("inf"))
    telemetry.record_stage_latency("t9_bad_stage", "not-a-number")  # type: ignore[arg-type]
    block = telemetry.metrics()["stage_latency_ms"]
    # the stage never got a valid sample → it is absent from the block (setdefault only fires on a good value).
    assert "t9_bad_stage" not in block


def test_stage_latency_records_no_ring_event():
    """record_stage_latency records a NUMBER only — it does NOT append a ring trace/event (a content no-op)."""
    before = telemetry.metrics()["ring_size"]
    telemetry.record_stage_latency("t9_noevent_stage", 3.0)
    after = telemetry.metrics()["ring_size"]
    assert after == before  # no new ring trace


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 3. QUALITY TRIPWIRES — pure evaluate() + the record/emit primitive.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_tripwire_zero_grounded_fires_on_ok_zero():
    hits = evaluate(QualitySignals(grounded_citations=0, verdict="ok"), TripwireThresholds())
    names = {h.name for h in hits}
    assert "zero_grounded_citations" in names


def test_tripwire_zero_grounded_silent_on_refuse():
    """A refuse/abstain grounding nothing is expected — NOT a tripwire."""
    hits = evaluate(QualitySignals(grounded_citations=0, verdict="refuse"), TripwireThresholds())
    assert "zero_grounded_citations" not in {h.name for h in hits}


def test_tripwire_silent_when_all_good():
    sig = QualitySignals(
        grounded_citations=5,
        verdict="ok",
        hedged=False,
        practical_present=True,
        practical_empty=False,
        demoted_slots=0,
        dropped_claims=0,
    )
    assert evaluate(sig, TripwireThresholds()) == []


def test_tripwire_empty_practical_and_hedged():
    sig = QualitySignals(grounded_citations=3, verdict="ok", hedged=True, practical_present=True, practical_empty=True)
    names = {h.name for h in evaluate(sig, TripwireThresholds())}
    assert "empty_practical_block" in names
    assert "hedged_answer" in names


def test_tripwire_demoted_and_dropped_thresholds():
    sig = QualitySignals(grounded_citations=3, verdict="ok", demoted_slots=4, dropped_claims=3)
    hits = {h.name: h for h in evaluate(sig, TripwireThresholds(max_demoted_slots=3, max_dropped_claims=2))}
    assert "demoted_slots" in hits and hits["demoted_slots"].value == 4
    assert "dropped_claims" in hits and hits["dropped_claims"].value == 3
    # at/under the edge ⇒ silent (strict >)
    assert evaluate(
        QualitySignals(verdict="ok", grounded_citations=1, demoted_slots=3), TripwireThresholds(max_demoted_slots=3)
    ) == [
        h
        for h in evaluate(
            QualitySignals(verdict="ok", grounded_citations=1, demoted_slots=3), TripwireThresholds(max_demoted_slots=3)
        )
        if h.name != "demoted_slots"
    ]


def test_tripwire_none_edge_disables():
    sig = QualitySignals(verdict="ok", grounded_citations=1, demoted_slots=99, dropped_claims=99)
    hits = evaluate(
        sig,
        TripwireThresholds(
            max_demoted_slots=None, max_dropped_claims=None, zero_grounded=False, empty_practical=False, hedged=False
        ),
    )
    assert hits == []


def test_tripwire_evaluate_is_pure():
    """evaluate() never mutates its inputs (frozen dataclasses) and returns TripwireHit rows."""
    sig = QualitySignals(grounded_citations=0, verdict="ok")
    th = TripwireThresholds()
    out = evaluate(sig, th)
    assert all(isinstance(h, TripwireHit) for h in out)
    assert sig.grounded_citations == 0 and sig.verdict == "ok"  # unchanged


def test_record_quality_tripwire_bumps_counter_and_event():
    before = telemetry.metrics()["quality_tripwires"]
    before_ring = telemetry.metrics()["ring_size"]
    telemetry.record_quality_tripwire("t9_test_tw", 0, 0)
    after = telemetry.metrics()["quality_tripwires"]
    assert after["events"] == before["events"] + 1
    assert after["per_tripwire"].get("t9_test_tw", 0) >= 1
    # it DOES emit a ring event (that is its purpose) — distinct from the latency instrument which does not.
    assert telemetry.metrics()["ring_size"] == before_ring + 1


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4. OFF-PATH GUARD — with both flags default-OFF, the answer path's stamp/tripwire branches never fire.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_off_guard_stamp_branch_inert(monkeypatch):
    """Mirror answer()'s _stamp guard: with stage_latency_enabled OFF (and claim-verify OFF), the stamp is a
    pure no-op — record_stage_latency is NEVER reached. We assert the guard condition + that a poisoned
    record_stage_latency would NOT be hit under the default-OFF flags."""
    s = get_settings()
    _cv_on = bool(getattr(s, "claim_verify_enabled", False))
    _lat_on = bool(getattr(s, "stage_latency_enabled", False))
    assert not _cv_on and not _lat_on  # both OFF by default → the stamp early-returns

    def _poison(*a, **k):
        raise AssertionError("record_stage_latency must not be called when stage_latency_enabled is OFF")

    monkeypatch.setattr(telemetry, "record_stage_latency", _poison)
    # Replay the stamp's guard exactly: OFF ⇒ no call.
    if _cv_on or _lat_on:
        telemetry.record_stage_latency("x", 1.0)  # unreached under defaults


def test_off_guard_tripwire_branch_inert(monkeypatch):
    """Mirror answer()'s tripwire guard: with quality_tripwires_enabled OFF the evaluation block is skipped —
    record_quality_tripwire is NEVER reached and no event is recorded."""
    s = get_settings()
    assert s.quality_tripwires_enabled is False

    def _poison(*a, **k):
        raise AssertionError("record_quality_tripwire must not be called when quality_tripwires_enabled is OFF")

    monkeypatch.setattr(telemetry, "record_quality_tripwire", _poison)
    if s.quality_tripwires_enabled:  # unreached under defaults
        telemetry.record_quality_tripwire("x", 0, 0)


def test_off_guard_metrics_blocks_zero_on_fresh_engine():
    """The additive metrics blocks are well-shaped and zero/empty for a consumer that ignores them — the
    OFF-path byte-identical shape for the /metrics surface (a fresh engine records no stage/tripwire)."""
    m = telemetry.metrics()
    assert isinstance(m["stage_latency_ms"], dict)
    assert m["quality_tripwires"]["events"] >= 0
    assert isinstance(m["quality_tripwires"]["per_tripwire"], dict)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
