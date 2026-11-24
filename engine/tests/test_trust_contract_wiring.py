"""B1a-08 — the CONTRACT + WIRING tests (DB-free).

Covers the contract author's deliverables (items 1–5 of the wiring task):
  • the additive `CitedProvision.trust` / `PythiaAnswer.obligation_register` contract (frozen TrustTag);
  • engine `_decorate` resolves a trust tag for EVERY connected (non-seed) provision;
  • the obligation register lists every must_surface rule (necessary AND sufficient);
  • the BEDROCK: a binding-cue provision under a MIS-MARKED preamble still resolves ≥ doc tier;
  • the CROSS-CORPUS vocab pin: the engine tier/temporal vocab == the app WP-001 CHECK vocab;
  • the WP-FUSION fold (a) contract stub (fusion.py is DEFERRED → documents the is_protected OR).

Fully DB-free: `_decorate` is driven through a fake NexusPort whose `fetch_provisions` returns
hand-built ProvisionRow-shaped rows, so NO live read touches the corpus (the enrichment is running).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest

from pythia.contract import CitedProvision, PythiaAnswer, TrustTag
from pythia.engine import PythiaEngine
from pythia.ports import ProvisionRow, resolve_trust

# the NeighborHit shape the engine merges/decorates (id, hops, score, relation_path).

AS_OF = date(2024, 1, 1)


# ─────────────────────────────────────────────────────────────────────── a DB-free fake door
@dataclass
class _Hit:
    """A NeighborHit-compatible stand-in (engine reads .provision_id/.hops/.score/.relation_path)."""

    provision_id: str
    hops: int = 1
    score: float = 0.5
    relation_path: list[str] = field(default_factory=lambda: ["references"])


def _prow(pid: str, **kw) -> ProvisionRow:
    """A ProvisionRow with safe in-force HARD_LAW defaults; override the trust inputs per test."""
    base = dict(
        id=pid,
        label=f"§ {pid}",
        text="text",
        jurisdiction="CZ",
        legal_hierarchy="ZAKON",
        valid_from=date(2000, 1, 1),
        valid_to=None,
        source_title="Act",
        external_id="1/2000",
        bindingness="HARD_LAW_ERGA_OMNES",
        is_authoritative=True,
        temporal_status="IN_FORCE",
        analysis_roles=[],
        parent_id=None,
        is_anchor_only=False,
        section_kind=None,
        kind_override=None,
        inherited_kind="operative",
        annulment_effect=None,
        annuls_target=False,
    )
    base.update(kw)
    return ProvisionRow(**base)


class _FakeNexus:
    """Stands in for NexusPort.fetch_provisions ONLY (the single read _decorate makes)."""

    def __init__(self, rows: dict[str, ProvisionRow]) -> None:
        self._rows = rows

    async def fetch_provisions(self, ids: list[str]) -> list[ProvisionRow]:
        return [self._rows[i] for i in ids if i in self._rows]


async def _decorate(rows: dict[str, ProvisionRow], hits: list[_Hit]) -> list[CitedProvision]:
    eng = PythiaEngine()
    eng.nexus = _FakeNexus(rows)  # swap the door — no DB, no model load
    return await eng._decorate(hits, as_of=AS_OF)


# ─────────────────────────────────────────────────────────────────────── contract shape (item 1)
def test_trusttag_is_frozen_and_all_optional():
    t = TrustTag()
    assert (t.bindingness_tier, t.section_kind, t.temporal_status) == (None, None, None)
    assert t.is_rule is False and t.must_surface is False and t.kind_source is None
    with pytest.raises(Exception):
        t.is_rule = True  # frozen → mutation raises


def test_citedprovision_trust_is_additive_default_none():
    cp = CitedProvision(provision_id="x")
    assert cp.trust is None  # additive, non-breaking: a bare CP has no fabricated tag
    assert PythiaAnswer().obligation_register == []


# ─────────────────────────────────────────────── EVERY connected (non-seed) provision carries a tag (item 3/5)
async def test_connected_non_seed_provision_carries_a_resolved_trust_tag():
    rows = {
        "seed1": _prow("seed1", analysis_roles=[2]),
        "conn1": _prow("conn1", analysis_roles=[2]),  # a graph-CONNECTED rule (relation_path=references)
    }
    hits = [_Hit("seed1", hops=0, relation_path=["seed"]), _Hit("conn1", hops=1, relation_path=["references"])]
    cited = await _decorate(rows, hits)
    assert len(cited) == 2
    conn = next(c for c in cited if c.provision_id == "conn1")
    assert conn.relation_path == ["references"]  # it is a CONNECTED (non-seed) hit
    assert conn.trust is not None  # … and it STILL carries a resolved tag
    assert conn.trust.bindingness_tier == "hard-law"
    assert conn.trust.is_rule is True
    # every decorated provision (seed AND connected) has a tag — none left None.
    assert all(c.trust is not None for c in cited)


# ─────────────────────────────────────── the obligation register lists EVERY must_surface rule (item 3/5)
async def test_obligation_register_lists_every_must_surface_rule_necessary_and_sufficient():
    rows = {
        "rule_inforce": _prow("rule_inforce", analysis_roles=[2]),  # in-force rule  → IN
        "rule_future": _prow("rule_future", analysis_roles=[3], valid_from=date(2030, 1, 1)),  # future rule → IN
        "rule_repealed": _prow(
            "rule_repealed", analysis_roles=[2], valid_to=date(2019, 1, 1), temporal_status="REPEALED"
        ),  # historical    → OUT
        "not_a_rule": _prow("not_a_rule", inherited_kind="definitions", analysis_roles=[]),  # not a rule → OUT
    }
    hits = [_Hit(pid) for pid in rows]
    eng = PythiaEngine()
    eng.nexus = _FakeNexus(rows)
    cited = await eng._decorate(hits, as_of=AS_OF)
    register = [c.provision_id for c in cited if c.trust is not None and c.trust.must_surface]
    # SUFFICIENT: every temporally-active rule is present.
    assert "rule_inforce" in register and "rule_future" in register
    # NECESSARY: nothing else leaks in (a historical rule and a non-rule are excluded).
    assert "rule_repealed" not in register and "not_a_rule" not in register
    assert set(register) == {"rule_inforce", "rule_future"}
    # the future-effective rule is surfaced (under the upcoming banner) — its status is flagged, not hidden.
    fut = next(c for c in cited if c.provision_id == "rule_future")
    assert fut.trust.temporal_status == "future-effective"


async def test_pythiaanswer_obligation_register_field_is_populated_end_to_end():
    # the full assembly the engine does after _decorate (the must_surface bridge → PythiaAnswer field).
    rows = {"r": _prow("r", analysis_roles=[2]), "d": _prow("d", inherited_kind="definitions")}
    eng = PythiaEngine()
    eng.nexus = _FakeNexus(rows)
    cited = await eng._decorate([_Hit("r"), _Hit("d")], as_of=AS_OF)
    ans = PythiaAnswer(
        cited_provisions=cited,
        obligation_register=[c.provision_id for c in cited if c.trust and c.trust.must_surface],
    )
    assert ans.obligation_register == ["r"]


# ─────────────────────────────────────── the BEDROCK: binding cue under a MIS-MARKED preamble (item 5)
async def test_binding_cue_under_mismarked_preamble_resolves_at_least_doc_tier():
    # the dangerous mis-classification: an obligation provision wrongly inheriting a `preamble_recital`
    # section. The per-provision-conservative guard + the doc floor mean it is NEVER weakened below the
    # hard-law doc tier — binding → soft is structurally impossible.
    rows = {"p": _prow("p", inherited_kind="preamble_recital", bindingness="HARD_LAW_ERGA_OMNES", analysis_roles=[2])}
    cited = await _decorate(rows, [_Hit("p")])
    t = cited[0].trust
    assert t.bindingness_tier == "hard-law"  # held at the doc floor, NOT downgraded to interpretive
    assert t.is_rule is True
    assert t.must_surface is True

    # contrast: a recital with NO binding cue IS the one allowed downgrade (→ interpretive).
    rows2 = {"q": _prow("q", inherited_kind="preamble_recital", analysis_roles=[])}
    cited2 = await _decorate(rows2, [_Hit("q")])
    assert cited2[0].trust.bindingness_tier == "interpretive"
    assert cited2[0].trust.is_rule is False


# ─────────────────────────────────────── cross-corpus vocab pin: engine tiers == app WP-001 CHECK (item 5)
# The app-side WP-001 CHECK literals (Polarius/migrations/013_trust_audit_export.sql) the engine output MUST
# equal exactly (hyphenated, lower-case). A drift here is the §4.2 failure-mode-6 trust-vocab-mismatch.
APP_WP001_TIER_VOCAB = {"hard-law", "oop", "soft-law", "interpretive"}
APP_WP001_TEMPORAL_VOCAB = {"in-force", "future-effective", "annulled", "superseded"}


def test_engine_tier_vocab_equals_app_wp001_check_vocab():
    from pythia.graph.trust_resolve import TIER_ORDER

    assert set(TIER_ORDER) == APP_WP001_TIER_VOCAB


def test_engine_temporal_vocab_equals_app_wp001_check_vocab():
    # exercise the resolver across every temporal branch and assert the emitted statuses are a SUBSET of the
    # app vocab (the resolver only ever emits these four; the app CHECK admits exactly these four + NULL).
    seen = set()
    seen.add(
        resolve_trust(
            {"bindingness": "HARD_LAW_ERGA_OMNES", "analysis_roles": [2], "valid_from": date(2000, 1, 1)}, AS_OF
        ).temporal_status
    )  # in-force
    seen.add(
        resolve_trust({"bindingness": "HARD_LAW_ERGA_OMNES", "valid_from": date(2030, 1, 1)}, AS_OF).temporal_status
    )  # future-effective
    seen.add(
        resolve_trust(
            {"bindingness": "HARD_LAW_ERGA_OMNES", "valid_to": date(2019, 1, 1), "temporal_status": "REPEALED"}, AS_OF
        ).temporal_status
    )  # superseded
    seen.add(
        resolve_trust(
            {
                "bindingness": "HARD_LAW_ERGA_OMNES",
                "valid_to": date(2019, 1, 1),
                "temporal_status": "REPEALED",
                "annuls_target": True,
            },
            AS_OF,
        ).temporal_status
    )  # annulled
    assert seen == APP_WP001_TEMPORAL_VOCAB
    assert seen <= APP_WP001_TEMPORAL_VOCAB


def test_resolved_tier_is_always_in_app_vocab():
    # any resolved tag's tier is a member of the app CHECK set (never an out-of-vocab token).
    for binding in (
        "HARD_LAW_ERGA_OMNES",
        "BINDING_OOP",
        "SOFT_LAW",
        "INTERPRETIVE_CASE_LAW",
        "BINDING_IN_PERSONAM",
        None,
        "GARBAGE",
    ):
        for kind in ("operative", "penalties", "definitions", "preamble_recital", "transitional_final", None):
            t = resolve_trust({"bindingness": binding, "inherited_kind": kind, "analysis_roles": []}, AS_OF)
            assert t.bindingness_tier in APP_WP001_TIER_VOCAB


# ─────────────────────────────────────── WP-FUSION fold (a) — WIRED contract (item 4, Phase-A scaffold)
def test_wp_fusion_is_protected_folds_in_trust_is_rule():
    """WP-FUSION Phase A is built: pythia.graph.fusion.is_protected ORs in the RESOLVED `trust.is_rule`
    (the authoritative rule-kind — section-level + per-provision override — not only the cue-based
    analysis_roles), keeping the must-fix #1 seed-path `.get()` guard. The resolver supplies is_rule and
    engine.retrieve threads it into fuse() via is_rule_by_id."""
    from pythia.graph.fusion import is_protected

    # (1) the resolver produces the is_rule the fold ORs in.
    t = resolve_trust({"bindingness": "HARD_LAW_ERGA_OMNES", "analysis_roles": [2]}, AS_OF)
    assert hasattr(t, "is_rule") and t.is_rule is True

    # the documented contract note (must-fix #1 .get() + fold (a) is_rule OR) is still present.
    from pythia.graph.trust_resolve import PROTECTED_RULE_NOTE

    assert "is_rule" in PROTECTED_RULE_NOTE and ".get()" in PROTECTED_RULE_NOTE

    # (2) fold (a) is WIRED: a provision with NO protected relation/role is STILL protected when the
    # resolved trust.is_rule is True; and the seed-path .get() guard holds (no KeyError on ['seed']/[]).
    assert is_protected([], None) is False
    assert is_protected(["seed"], None) is False  # must-fix #1: no KeyError, not protected
    assert is_protected([], None, is_rule=True) is True  # fold (a): resolved rule-kind floors it
    assert is_protected(["seed"], [], is_rule=True) is True
