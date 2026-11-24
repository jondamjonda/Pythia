"""VL-B-09 — the DARK, APPEND-NEVER-DISPLACE promotion (gate #2, flips LAST).

Pins:
  (a) FLAG — claim_verify_promote_enabled + verify_promote_cap default OFF/2.
  (b) APPEND-NEVER-DISPLACE — the original prov tuples are a strict PREFIX (same objects, same order, none
      removed / reordered) of the extended set; only missed_in_corpus provisions are appended.
  (c) CAP — > verify_promote_cap missed_in_corpus findings ⇒ exactly ≤ cap promotions.
  (d) NOT-IN-CORPUS is never promoted; an already-retrieved pid is never re-promoted (append-never-duplicate).
  (e) LABELLING — every promoted record carries promoted=True / needs_lawyer_review=True.
  (f) §X→§X+1 e2e-ish — a draft cites a missed NEIGHBOUR (§298) not in the retrieved Map; the probe row
      resolves it to a corpus pid; promotion appends it + RE-VERIFIES (via the REAL verify.verify over the
      extended tuples) ⇒ the neighbour becomes grounded, the original tuples untouched.
  (g) OFF byte-identity — the sub-flag OFF ⇒ promote_repair never fires (no promoted_evidence).

The re-verify is injected (a deterministic fake, or the REAL pure verify.verify) — no live DB.
"""

from __future__ import annotations

from pythia.config import PythiaSettings
from pythia.reasoning.claim_repair import (
    build_promoted_tuples,
    promote_repair,
    promote_targets,
)
from pythia.reasoning.verify import verify as verify_answer

# ─────────────────────────────────────────── (a) flags default OFF/2 ──────────────────────────────────────


def test_promote_flag_defaults():
    s = PythiaSettings()
    assert s.claim_verify_promote_enabled is False
    assert s.verify_promote_cap == 2


# ───────────────────────────────── (b) APPEND-NEVER-DISPLACE — base is a strict prefix ────────────────────


def _row(pid, ext, sec, *, missed=True):
    r = {
        "tag": f"{ext} §{sec}",
        "ext": ext,
        "sec": sec,
        "classification": "missed_in_corpus" if missed else "not_in_corpus",
    }
    if missed:
        r["provision_id"] = pid
    return r


def test_append_never_displace_base_is_prefix():
    base = [("p1", "240/2013", "§ 297"), ("p2", "89/2012", "§ 318")]
    ev = [_row("p9", "240/2013", "298")]
    extended, records = build_promoted_tuples(base, ev)
    # base is a STRICT PREFIX — same objects, same order, nothing removed/reordered
    assert extended[: len(base)] == base
    assert extended[0] is base[0] and extended[1] is base[1]
    # exactly one appended tuple, carrying the promoted (pid, ext, §sec)
    assert extended[len(base) :] == [("p9", "240/2013", "§298")]
    assert [r["provision_id"] for r in records] == ["p9"]
    # the caller's list was NOT mutated
    assert base == [("p1", "240/2013", "§ 297"), ("p2", "89/2012", "§ 318")]


# ─────────────────────────────────────────────── (c) CAP ──────────────────────────────────────────────────


def test_cap_bounds_promotions():
    ev = [_row(f"p{i}", "240/2013", str(300 + i)) for i in range(5)]
    targets = promote_targets(ev, cap=2)
    assert len(targets) == 2
    extended, records = build_promoted_tuples([], promote_targets(ev, cap=2))
    assert len(records) == 2
    assert len(extended) == 2


# ───────────────────────── (d) not_in_corpus never promoted; already-retrieved never re-promoted ──────────


def test_not_in_corpus_never_promoted():
    ev = [_row("p1", "240/2013", "297", missed=False)]  # not_in_corpus (no provision_id)
    assert promote_targets(ev) == []  # filtered out at the target-selection gate
    # the pipeline always feeds build_promoted_tuples the FILTERED targets ⇒ nothing appended
    extended, records = build_promoted_tuples([], promote_targets(ev))
    assert extended == [] and records == []


def test_already_retrieved_never_repromoted():
    # the resolved pid is ALREADY in the retrieved Map ⇒ append-never-duplicate: no new tuple, no record.
    base = [("p9", "240/2013", "§298")]
    ev = [_row("p9", "240/2013", "298")]
    extended, records = build_promoted_tuples(base, promote_targets(ev))
    assert extended == base and records == []


# ────────────────────────────────────────── (e) labelling ─────────────────────────────────────────────────


def test_promoted_records_labelled_for_lawyer():
    _extended, records = build_promoted_tuples([], promote_targets([_row("p9", "240/2013", "298")]))
    assert len(records) == 1
    r = records[0]
    assert r["promoted"] is True and r["needs_lawyer_review"] is True
    assert r["provision_id"] == "p9" and r["ext"] == "240/2013" and r["sec"] == "298"


# ──────────────────────────── (f) §X→§X+1 e2e-ish over the REAL pure verify ───────────────────────────────


def test_promote_repair_missed_neighbour_becomes_grounded():
    """The draft cites §297 (retrieved) AND the missed NEIGHBOUR §298 (not retrieved). The probe row resolves
    §298 to corpus pid 'p298'. Promotion APPENDS it and RE-VERIFIES over the REAL verify.verify: §298 is now
    grounded, §297 stays grounded, and the base tuples are untouched (append-never-displace)."""
    draft = "Podle [240/2013 § 297] platí X a podle [240/2013 § 298] platí Y."
    base = [("p297", "240/2013", "§ 297")]  # only §297 was retrieved
    ev = [_row("p298", "240/2013", "298")]  # §298 EXISTS in corpus but was missed
    out = promote_repair(draft, base, ev, verify_answer, cap=2)
    assert out is not None
    # append-never-displace: base is the prefix of the extended tuples
    assert out["extended_tuples"][: len(base)] == base
    assert out["extended_tuples"][len(base) :] == [("p298", "240/2013", "§298")]
    # the promoted neighbour is now grounded on the re-verify
    assert "p298" in out["verdict"].grounded_provision_ids
    assert "p297" in out["verdict"].grounded_provision_ids  # the originally-retrieved cite still grounds
    assert out["newly_grounded"] == ["p298"]
    assert out["n_promoted"] == 1 and out["n_newly_grounded"] == 1
    assert out["promoted"][0]["needs_lawyer_review"] is True


def test_promote_repair_noop_when_no_missed_findings():
    # only not_in_corpus findings ⇒ nothing to promote ⇒ None (caller does nothing → byte-identical).
    ev = [_row("p1", "240/2013", "297", missed=False)]
    called = {"n": 0}

    def _reverify(_d, _t):
        called["n"] += 1
        return verify_answer(_d, _t)

    assert promote_repair("draft", [("p1", "240/2013", "§ 297")], ev, _reverify) is None
    assert called["n"] == 0  # re-verify is NEVER called on a no-op


# ─────────────────────────────────── (g) OFF byte-identity (answer.py seam) ───────────────────────────────


def test_off_no_promotion_seam_inert(monkeypatch):
    """With the master + probe + promote flags OFF (the defaults) the promotion primitive is never reached.
    Pin it structurally: promote_repair over an EMPTY verify_evidence (what an OFF probe yields) is a no-op,
    and its re-verify is never called — so the answer path stays byte-identical."""
    called = {"n": 0}

    def _reverify(_d, _t):
        called["n"] += 1
        return verify_answer(_d, _t)

    assert promote_repair("draft", [("p1", "240/2013", "§ 297")], [], _reverify) is None
    assert called["n"] == 0
