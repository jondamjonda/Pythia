"""VL-B-08 — the verification-only citation resolver + retrieval-gap probe (increment 1).

Pins:
  (a) §-GRAIN AGREEMENT — the probe's target parse and NexusPort.resolve_citation use the SAME `_SEC_RE`
      §-grammar verify.ground() uses (a shared-grammar pin: the same (ext,§) string collapses identically
      on both sides — no second regex).
  (b) CAP — >8 act-qualified ungrounded cites ⇒ exactly ≤8 resolve_citation calls (port-spy).
  (c) MISSED-IN-CORPUS — an ungrounded cite that RESOLVES ⇒ classified missed_in_corpus in verify_evidence
      + NEVER added to cited_provisions; a cite that does NOT resolve ⇒ stays not_in_corpus.
  (d) OFF BYTE-IDENTITY — the sub-flag OFF ⇒ zero resolve_citation calls + no verify_evidence key.

A stub NexusPort supplies resolve_citation (no live DB).
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.reasoning.gap_probe import (
    GAP_PROBE_CAP,
    build_verify_evidence,
    parse_probe_targets,
)
from pythia.reasoning.verify import _sec_num

# ─────────────────────────────────────────── flag exists, default OFF ─────────────────────────────────────


def test_gap_probe_flag_default_off():
    s = PythiaSettings()
    assert s.claim_verify_gap_probe_enabled is False


# ─────────────────────────────────────── (a) §-grain shared-grammar pin ────────────────────────────────────


def test_probe_targets_use_shared_sec_grammar():
    """parse_probe_targets normalizes the §-number with the SAME parse verify uses. verify.ground() emits an
    act-qualified ungrounded cite as f'{act} §{sec}' where sec is ALREADY _SEC_RE-normalized — so the target §
    equals _sec_num of that same section string, both sides agreeing on what '§ 318(1)' means ('318')."""
    # The emit shape carries the normalized §; the pin is that our re-parse == verify's _sec_num on the raw §.
    for raw_sec, ext in (("§ 297", "240/2013"), ("§ 318(1)", "89/2012"), ("čl. 27a", "32014L0065")):
        want = _sec_num(raw_sec)
        assert want is not None
        targets = parse_probe_targets([f"{ext} §{want}"])
        assert targets == [(ext, want)]  # same (ext,§) both sides


def test_probe_targets_skip_bare_and_ambiguous():
    """A BARE ('§297') or AMBIGUOUS ('§297 (ambiguous act)') ungrounded cite carries NO act to probe → skipped
    (there is nothing to resolve without an act external_id). Only act-qualified cites become probe targets."""
    assert parse_probe_targets(["§297", "§297 (ambiguous act)", "  "]) == []
    assert parse_probe_targets(["240/2013 §297", "§18"]) == [("240/2013", "297")]


def test_probe_targets_dedup_before_cap():
    """De-dup happens before the cap: the same (ext,§) cited twice is ONE target."""
    assert parse_probe_targets(["1/2000 §5", "1/2000 §5", "1/2000 §6"]) == [("1/2000", "5"), ("1/2000", "6")]


# ─────────────────────────────────────────── (c pure) classification ──────────────────────────────────────


def test_build_verify_evidence_classifies_missed_vs_not_in_corpus():
    rows = build_verify_evidence([("240/2013", "297", "pid-abc"), ("89/2012", "500", None)])
    assert rows == [
        {
            "tag": "240/2013 §297",
            "ext": "240/2013",
            "sec": "297",
            "classification": "missed_in_corpus",
            "provision_id": "pid-abc",
        },
        {"tag": "89/2012 §500", "ext": "89/2012", "sec": "500", "classification": "not_in_corpus"},
    ]
    # a not_in_corpus row NEVER carries a provision_id (no chip target)
    assert "provision_id" not in rows[1]


# ─────────────────────────────────────── answer() harness (no LLM / DB / model) ────────────────────────────


class _SpyPort:
    """Stub NexusPort.resolve_citation (no live DB). `resolves` maps (ext, sec) → a fake pid (else None).
    Records every call so the cap + zero-call invariants are spy-checkable."""

    def __init__(self, resolves: dict[tuple[str, str], str]):
        self.resolves = resolves
        self.calls: list[tuple[str, str]] = []

    async def resolve_citation(self, ext, sec):
        self.calls.append((ext, sec))
        return self.resolves.get((ext, sec))


def _install_harness(monkeypatch, settings, *, draft: str, ret_label: str = "§ 297", ret_ext: str = "240/2013"):
    """Stub answer.py's LLM / engine / fetch so answer() runs with no LLM/DB/model. `draft` is the injected
    section-headed answer; the ONE retrieved provision (p1) carries `ret_label` in act `ret_ext`."""
    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return draft

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label=ret_label, score=0.82)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    _FULL = "Depozitář musí " + "X" * 900 + " oznámit do 15 dnů " + "Y" * 400

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        m = {
            "id": "p1",
            "label": ret_label,
            "ext": ret_ext,
            "title": "ZISIF",
            "jurisdiction": "CZ",
            "txt": _FULL[:700],
            "ctx_txt": _FULL[:700],
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
            "legal_hierarchy": "STATUTE",
            "pit_status": None,
            "vector_stale": None,
        }
        if keep_full:
            m["full_txt"] = _FULL
        return {"p1": m}

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)


# A draft that GROUNDS the retrieved [240/2013 § 297] AND makes ONE act-qualified ungrounded cite to a §
# that is NOT in the retrieved set (89/2012 § 500) — the probe target.
_DRAFT_ONE_MISS = (
    "## Legislativní rámec\n"
    "Podle [240/2013 § 297] platí povinnost depozitáře. "
    "Dále viz [89/2012 § 500].\n"
    "## Hodnocení\nOK."
)
_Q = "Jaké povinnosti má depozitář podle § 297?"


# ─────────────────────────────────────── (d) OFF byte-identity (spy) ───────────────────────────────────────


async def test_probe_off_no_calls_no_key(monkeypatch):
    """Sub-flag OFF ⇒ zero resolve_citation calls + no verify_evidence audit key (byte-identical)."""
    port = _SpyPort({("89/2012", "500"): "pid-x"})
    monkeypatch.setattr(ans, "NexusPort", lambda: port, raising=True)
    # claim_verify ON but the gap-probe sub-flag OFF (the default)
    s = PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow")
    assert s.claim_verify_gap_probe_enabled is False
    _install_harness(monkeypatch, s, draft=_DRAFT_ONE_MISS)
    res = await ans.answer(_Q, method="cte")
    assert port.calls == []  # zero probes
    assert "verify_evidence" not in res.audit_trace  # no key → byte-identical


async def test_probe_off_when_master_off(monkeypatch):
    """The sub-flag is INERT while the claim-verify MASTER is OFF — the whole claim-verify region is skipped."""
    port = _SpyPort({("89/2012", "500"): "pid-x"})
    monkeypatch.setattr(ans, "NexusPort", lambda: port, raising=True)
    # sub-flag ON but master OFF ⇒ region never entered
    s = PythiaSettings(claim_verify_gap_probe_enabled=True)
    assert s.claim_verify_enabled is False
    _install_harness(monkeypatch, s, draft=_DRAFT_ONE_MISS)
    res = await ans.answer(_Q, method="cte")
    assert port.calls == []
    assert "verify_evidence" not in res.audit_trace


# ─────────────────────────── (c) missed-in-corpus fixture + never-a-chip through answer() ──────────────────


async def test_probe_missed_in_corpus_and_never_a_chip(monkeypatch):
    """ON: an ungrounded cite that RESOLVES ⇒ classified missed_in_corpus in verify_evidence + carries the
    resolved pid, and that pid is NEVER added to cited_provisions / citations_map (verification-only)."""
    port = _SpyPort({("89/2012", "500"): "pid-missed"})
    monkeypatch.setattr(ans, "NexusPort", lambda: port, raising=True)
    s = PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow", claim_verify_gap_probe_enabled=True)
    _install_harness(monkeypatch, s, draft=_DRAFT_ONE_MISS)
    res = await ans.answer(_Q, method="cte")

    assert port.calls == [("89/2012", "500")]  # exactly the one act-qualified ungrounded cite
    ve = res.audit_trace["verify_evidence"]
    assert ve == [
        {
            "tag": "89/2012 §500",
            "ext": "89/2012",
            "sec": "500",
            "classification": "missed_in_corpus",
            "provision_id": "pid-missed",
        }
    ]
    # VERIFICATION-ONLY: the missed pid is NEVER a served chip
    cited_ids = {cp.provision_id for cp in res.cited_provisions}
    assert "pid-missed" not in cited_ids
    assert all(c.get("provision_id") != "pid-missed" for c in res.citations_map)
    # the grounded [240/2013 § 297] chip IS served (p1) — the probe added nothing to the served set
    assert "p1" in cited_ids


async def test_probe_not_in_corpus_stays_hard_fail(monkeypatch):
    """ON: an ungrounded cite that does NOT resolve ⇒ classified not_in_corpus (the existing fabricated
    hard-fail, unchanged) with NO provision_id."""
    port = _SpyPort({})  # resolves nothing
    monkeypatch.setattr(ans, "NexusPort", lambda: port, raising=True)
    s = PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow", claim_verify_gap_probe_enabled=True)
    _install_harness(monkeypatch, s, draft=_DRAFT_ONE_MISS)
    res = await ans.answer(_Q, method="cte")

    assert port.calls == [("89/2012", "500")]
    ve = res.audit_trace["verify_evidence"]
    assert ve == [{"tag": "89/2012 §500", "ext": "89/2012", "sec": "500", "classification": "not_in_corpus"}]
    assert "provision_id" not in ve[0]


# ─────────────────────────────────────────── (b) cap (port-spy) ───────────────────────────────────────────


async def test_probe_cap_at_most_8_calls(monkeypatch):
    """>8 DISTINCT act-qualified ungrounded cites ⇒ exactly ≤ GAP_PROBE_CAP resolve_citation calls."""
    # A draft that grounds the retrieved cite AND makes 12 distinct ungrounded act-qualified cites.
    misses = " ".join(f"[9{n:02d}/2010 § {n}]" for n in range(12))
    draft = "## Legislativní rámec\nPodle [240/2013 § 297] platí. " + misses + "\n## Hodnocení\nOK."
    port = _SpyPort({})  # none resolve — irrelevant to the cap count
    monkeypatch.setattr(ans, "NexusPort", lambda: port, raising=True)
    s = PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow", claim_verify_gap_probe_enabled=True)
    _install_harness(monkeypatch, s, draft=draft)
    res = await ans.answer(_Q, method="cte")

    assert GAP_PROBE_CAP == 8
    assert len(port.calls) <= GAP_PROBE_CAP  # cap enforced
    assert len(port.calls) == GAP_PROBE_CAP  # 12 distinct misses ⇒ exactly the 8-cap
    assert len(res.audit_trace["verify_evidence"]) == GAP_PROBE_CAP


# ─────────────────────────────────────── resolve_citation shared-grammar unit ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_citation_shares_sec_grammar_via_fetch(monkeypatch):
    """NexusPort.resolve_citation matches a candidate provision by _sec_num(label) — the SAME parse verify
    uses. A stub connection returns act 240/2013's provisions; '§ 318(1)' resolves to the row whose label is
    '§ 318' (both collapse to '318'), and a §-number absent from the act returns None. No live DB."""
    from pythia import ports

    class _Conn:
        async def fetch(self, sql, ext):
            assert ext == "240/2013"
            return [{"id": "pid-297", "label": "§ 297"}, {"id": "pid-318", "label": "§ 318"}]

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    async def _fake_pool():
        return _Pool()

    monkeypatch.setattr(ports, "get_pool", _fake_pool, raising=True)
    port = ports.NexusPort()
    # '§ 318(1)' → '318' (shared _SEC_RE §-grain) → the '§ 318' row
    assert await port.resolve_citation("240/2013", "§ 318(1)") == "pid-318"
    assert await port.resolve_citation("240/2013", "297") == "pid-297"
    # a §-number the act does not carry ⇒ None (corpus-existence miss)
    assert await port.resolve_citation("240/2013", "999") is None
    # empty ext / sec ⇒ None with NO DB read attempted
    assert await port.resolve_citation("", "297") is None
    assert await port.resolve_citation("240/2013", "") is None
