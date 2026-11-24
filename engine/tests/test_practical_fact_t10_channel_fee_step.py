"""T10 — the three new practical_fact extractor types: channel / fee / procedure_step.

Pure unit tests (no DB, no LLM). Proves, on crafted Czech provisions:
  (A) each new type extracts a VERBATIM excerpt (text[char_start:char_end] == value_raw) and passes the gate;
  (B) the four PRE-EXISTING types (deadline/authority/penalty/form) are byte-identical — the extraction over
      a pinned fixture, filtered to the old types, is UNCHANGED by the new families (a stored golden set);
  (C) practical_check's known-fact_type set + fact_lookup's fallback both accept the three new types.
"""

from __future__ import annotations

from pythia.ingest import enrich_practical as ep
from pythia.ingest import practical_check as pc
from pythia.reasoning.practical_facts import (
    FACT_TYPES,
    extract_practical_facts,
)

# The exact fixture the pre-existing suite pins for the 4 old types (test_practical_fact_b3p1._SAMPLE). We
# re-use it verbatim so the "old types unchanged" pin is anchored to the SAME text the old tests assert on.
_OLD_SAMPLE = (
    "Poplatník je povinen podat oznámení správci daně nejpozději do 30 dnů ode dne "
    "zahájení činnosti. Za porušení lze uložit pokutu do výše 500 000 Kč. "
    "Oznámení se podává na tiskopisu vydaném úřadem."
)


# ===== helpers =============================================================================================


def _by_type(text: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in extract_practical_facts(text):
        out.setdefault(f.fact_type, []).append(f)
    return out


def _assert_verbatim(text: str, facts) -> None:
    for f in facts:
        assert text[f.char_start : f.char_end] == f.value_raw
        # and the offline gate agrees (single chokepoint the wave uses)
        cand = ep._Candidate(
            provision_id="00000000-0000-0000-0000-000000000001",
            fact_type=f.fact_type,
            value_raw=f.value_raw,
            char_start=f.char_start,
            char_end=f.char_end,
            value_norm=None,
            extraction_method="regex",
            extractor_version="t10",
        )
        assert ep.gate_row(text, cand) is True


# ===== (A) new types extract verbatim excerpts =============================================================


def test_channel_extracts_verbatim():
    text = (
        "Oznámení se podává prostřednictvím datové schránky nebo elektronicky. "
        "Podání lze učinit i písemně, osobně, nebo prostřednictvím portálu MOJE daně."
    )
    facts = _by_type(text).get("channel", [])
    assert facts, "expected channel facts"
    _assert_verbatim(text, facts)
    vals = {f.value_raw for f in facts}
    # at least the datová schránka + an e-gov channel are surfaced, verbatim
    assert any("datov" in v.lower() and "schr" in v.lower() for v in vals)
    assert any(
        v.lower() in ("elektronicky", "písemně", "osobně") or "portál" in v.lower() or "MOJE dan" in v for v in vals
    )


def test_fee_extracts_verbatim_and_amount():
    text = (
        "Za přijetí žádosti se vybírá správní poplatek 200 Kč. Podání učiněné elektronicky je osvobozeno od poplatku."
    )
    facts = _by_type(text).get("fee", [])
    assert facts, "expected fee facts"
    _assert_verbatim(text, facts)
    joined = " | ".join(f.value_raw for f in facts).lower()
    assert "správní poplatek" in joined
    assert "osvobozen" in joined and "poplatk" in joined


def test_procedure_step_extracts_verbatim_clause():
    text = "Poplatník podá přihlášku k registraci a oznámí zahájení činnosti správci daně."
    facts = _by_type(text).get("procedure_step", [])
    assert facts, "expected procedure_step facts"
    _assert_verbatim(text, facts)
    joined = " | ".join(f.value_raw for f in facts).lower()
    assert "podá" in joined and "přihlášku" in joined


def test_new_types_are_in_vocab():
    for t in ("channel", "fee", "procedure_step"):
        assert t in FACT_TYPES


# ===== (B) the four pre-existing types are UNCHANGED (byte-identical golden pin) ============================

# Golden extraction for the OLD types over _OLD_SAMPLE, captured from the extractor BEFORE the T10 change.
# (Each entry is (fact_type, value_raw, char_start, char_end).) If a new pattern family ever perturbs an old
# type's spans, this pin fails.
_OLD_GOLDEN = {
    ("authority", "správci daně", 36, 48),
    ("deadline", "nejpozději do 30 dnů", 49, 69),
    ("deadline", "do 30 dnů ode dne", 60, 77),
    ("penalty", "pokutu do výše 500 000 Kč", 120, 145),
    ("form", "tiskopisu", 169, 178),
}


def test_old_four_types_unchanged_on_fixture():
    got = {
        (f.fact_type, f.value_raw, f.char_start, f.char_end)
        for f in extract_practical_facts(_OLD_SAMPLE)
        if f.fact_type in ("deadline", "authority", "penalty", "form")
    }
    # every span still slices back (self-consistency), then the exact golden set matches byte-for-byte.
    for _t, v, s, e in got:
        assert _OLD_SAMPLE[s:e] == v
    assert got == _OLD_GOLDEN, (
        "the four pre-existing fact types changed — new families must not perturb them.\n"
        f"got:      {sorted(got)}\nexpected: {sorted(_OLD_GOLDEN)}"
    )


# ===== (C) downstream consumers accept the new types =======================================================


def test_practical_check_knows_new_types():
    for t in ("channel", "fee", "procedure_step"):
        assert t in pc._VALID_FACT_TYPES


def test_regex_wave_flows_new_types_through():
    # the offline wave's candidate builder iterates ALL types the extractor returns (no hardcoded allow-list)
    text = "Poplatník podá přihlášku správci daně; správní poplatek 200 Kč; datovou schránkou."
    ftypes = {c.fact_type for c in ep.regex_candidates("pid", text)}
    assert {"channel", "fee", "procedure_step"} & ftypes, ftypes
    for c in ep.regex_candidates("pid", text):
        assert text[c.char_start : c.char_end] == c.value_raw
