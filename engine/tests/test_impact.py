"""C1 (MACRO) — tests for amendment_impact.compute_impact against a FAKE async conn (no live DB).

The two safety-critical behaviours this wave must guarantee:
  • the NO-OP FILTER: a '+0 ~0 -0' change_summary (the live DUPLICATE rows, e.g. act 344/2004) ⇒ None — no
    impact, no false "obligation changed" alert. Also a missing/unparseable summary ⇒ None.
  • ranking by centrality DESC + obligation flagging via the deterministic trust resolver, on SYNTHETIC
    affected-provision rows (so NO live pythia read/write occurs).
The persist path is NOT exercised against a live DB; the default (flag off) path returns WITHOUT writing.
"""

from __future__ import annotations

from datetime import date

from pythia.graph import impact as im


# ───────────────────────────────────────────────────────────────── parse_change_summary (the no-op filter)
def test_parse_change_summary_noop_returns_none():
    """The '+0 ~0 -0' no-op (and its '(provisions N→N)' tail) must parse to None — the false-alert guard."""
    assert im.parse_change_summary("+0 ~0 -0 (provisions 200→200)") is None
    assert im.parse_change_summary("+0 ~0 -0") is None


def test_parse_change_summary_missing_or_unparseable_returns_none():
    assert im.parse_change_summary(None) is None
    assert im.parse_change_summary("") is None
    assert im.parse_change_summary("garbage with no counts") is None


def test_parse_change_summary_real_change():
    assert im.parse_change_summary("+1 ~2 -0 (provisions 200→201)") == {"added": 1, "changed": 2, "removed": 0}


# ─────────────────────────────────────────────────────────────────────── a FAKE async conn (no live DB)
class _FakeConn:
    """fetchrow → the document_version row; fetch → the synthetic affected-provision rows. execute is a
    HARD TRIP-WIRE: the default (flag-off) path must NEVER write, so any execute fails the test."""

    def __init__(self, dv_row, affected_rows=None):
        self._dv_row = dv_row
        self._affected = affected_rows or []

    async def fetchrow(self, *_a, **_k):
        return self._dv_row

    async def fetch(self, *_a, **_k):
        return self._affected

    async def execute(self, *_a, **_k):  # pragma: no cover - asserted unreachable in the default path
        raise AssertionError("compute_impact wrote to the DB on the default (flag-off) path")


def _dv(change_summary):
    return {
        "id": "dv-uuid",
        "external_id": "344/2004",
        "version_path": "vp/1",
        "change_summary": change_summary,
        "n_provisions": 200,
        "valid_from": date(2024, 1, 1),
        "valid_to": None,
    }


def _prov(pid, label, centrality, *, obligation):
    """A synthetic affected-provision row (dict) carrying exactly the columns resolve_trust + the impact code
    read. obligation=True ⇒ a binding operative rule with an obligation cue (analysis_roles=[2]) that is
    in-force ⇒ must_surface True; obligation=False ⇒ a plain definitions provision, no cue ⇒ not a rule."""
    return {
        "id": pid,
        "label": label,
        "centrality": centrality,
        "bindingness": "HARD_LAW_ERGA_OMNES",
        "section_kind": "operative" if obligation else "definitions",
        "kind_override": None,
        "inherited_kind": "operative" if obligation else "definitions",
        "deontic_modal": "musí" if obligation else None,
        "deontic_actor": None,
        "deontic_action": None,
        "deontic_condition": None,
        "valid_from": date(2000, 1, 1),
        "valid_to": None,
        "temporal_status": "IN_FORCE",
        "annulment_effect": None,
        "annuls_target": False,
        "analysis_roles": [2] if obligation else [],
    }


# ───────────────────────────────────────────────────────────────────────────────── the no-op filter (end to end)
async def test_compute_impact_noop_change_summary_returns_none():
    """The headline safety guarantee: a '+0 ~0 -0' lineage row ⇒ None (no impact, no false alert)."""
    conn = _FakeConn(_dv("+0 ~0 -0 (provisions 200→200)"))
    assert await im.compute_impact(conn, "dv-uuid") is None


async def test_compute_impact_missing_document_version_returns_none():
    conn = _FakeConn(dv_row=None)
    assert await im.compute_impact(conn, "missing-uuid") is None


# ───────────────────────────────────────────────────────────── ranking by centrality + obligation flagging
async def test_compute_impact_ranks_by_centrality_and_flags_obligations():
    """A real '+1 ~1 -0' change with synthetic affected rows: ranked by centrality DESC (the SQL pre-sorts;
    we assert order is preserved) and only the binding operative rule is flagged as an obligation."""
    affected = [
        _prov("p-hi", "§ 10", 0.9, obligation=True),  # high-centrality obligation
        _prov("p-mid", "§ 20", 0.5, obligation=False),  # mid definitions (not a rule)
        _prov("p-lo", "§ 30", 0.1, obligation=True),  # low-centrality obligation
    ]
    conn = _FakeConn(_dv("+1 ~1 -0 (provisions 200→201)"), affected_rows=affected)
    out = await im.compute_impact(conn, "dv-uuid")
    assert out is not None
    # centrality DESC order preserved from the query
    assert [p["id"] for p in out["affected_provisions"]] == ["p-hi", "p-mid", "p-lo"]
    # only the two binding operative rules are obligations (the definitions provision is NOT)
    assert {o["id"] for o in out["affected_obligations"]} == {"p-hi", "p-lo"}
    assert "2 obligations" in out["summary"]
    assert out["changed"] == {"added": 1, "changed": 1, "removed": 0}


async def test_compute_impact_default_path_does_not_write():
    """Default (flag version_diff_enabled is False) ⇒ NO persist. The fake conn's execute is a trip-wire that
    raises if reached, so a clean return proves the read-only default holds."""
    conn = _FakeConn(_dv("+1 ~0 -0 (provisions 200→201)"), affected_rows=[_prov("p1", "§ 1", 0.4, obligation=True)])
    out = await im.compute_impact(conn, "dv-uuid")  # must not raise (no execute called)
    assert out is not None and out["external_id"] == "344/2004"
