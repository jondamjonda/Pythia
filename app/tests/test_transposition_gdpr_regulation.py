"""B1a-11 — GDPR (32016R0679) ↔ 110/2019 Sb. transposition wording.

GDPR is a *regulation*, not a directive: it is directly applicable and is NOT
transposed. Czech Act 110/2019 Sb. only *supplements* it where the regulation
leaves room for national rules. The bug class this test guards against is the
note renderer calling GDPR an "EU directive transposed by the Czech text" once
the GDPR↔110/2019 pair is added to TRANSPOSITION_MAP (the EU-side lookup would
otherwise win over the directly-applicable branch and emit directive wording).

DB-free: `transposition_note_for_provision` opens a `session_scope()` solely to
map provision_id → source_document.external_id. We patch that seam with a fake
async session that returns a chosen external_id, so no DB / network is touched.
The pattern mirrors tests/test_cascade_killswitch_abort.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

from regsuper.analysis import transposition_map as tm

GDPR_CELEX = "32016R0679"
GDPR_CZ = "110/2019"


# --------------------------------------------------------------------------- fakes
def _fake_session_scope_returning(external_id: str | None):
    """Build a fake `session_scope` whose single SELECT yields `external_id`
    in column 0 (or no row when external_id is None)."""

    class _Result:
        def first(self):
            return None if external_id is None else (external_id,)

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    @asynccontextmanager
    async def _scope():
        yield _Session()

    return _scope


# --------------------------------------------------------------------------- static map
def test_gdpr_pair_appended_to_map():
    pair = tm.transposition_for_eu(GDPR_CELEX)
    assert pair is not None
    assert pair.eu_celex == GDPR_CELEX
    assert pair.cz_external_id == GDPR_CZ
    # The reverse lookup is symmetric.
    assert tm.transposition_for_cz(GDPR_CZ) is pair


def test_gdpr_remains_directly_applicable():
    # GDPR must stay in DIRECT_REGULATIONS — the note renderer keys off this to
    # pick regulation-correct wording, and a regulation is directly applicable.
    assert tm.is_directly_applicable(GDPR_CELEX) is True
    assert any(c == GDPR_CELEX for c, _ in tm.DIRECT_REGULATIONS)


def test_map_is_an_immutable_tuple():
    # Frozen-tuple append: still a tuple, dataclass is still frozen.
    assert isinstance(tm.TRANSPOSITION_MAP, tuple)


# --------------------------------------------------------------------------- note wording
async def test_gdpr_note_contains_no_directive_string():
    """The load-bearing assertion: the GDPR note must NEVER call GDPR a directive."""
    scope = _fake_session_scope_returning(GDPR_CELEX)
    with patch.object(tm, "session_scope", scope):
        note = await tm.transposition_note_for_provision(uuid4())
    assert note is not None
    # HARD requirement (task B1a-11): the GDPR note must NOT call it a directive.
    assert "directive" not in note.lower()
    # It may say "NOT transposed" to clarify the regulation case, but it must
    # never *assert* GDPR is transposed. The only allowed occurrence of the
    # token is in the explicit negation.
    lowered = note.lower()
    assert "is transposed" not in lowered
    assert "transposes" not in lowered
    if "transposed" in lowered:
        assert "not transposed" in lowered
    # And it says the right thing: directly applicable + 110/2019 supplements.
    assert "regulation" in lowered
    assert "directly applicable" in lowered
    assert GDPR_CZ in note
    assert "supplement" in lowered


async def test_gdpr_cz_side_note_says_supplements_not_transposes():
    """The CZ side (110/2019) must also avoid 'transposes' — it supplements."""
    scope = _fake_session_scope_returning(GDPR_CZ)
    with patch.object(tm, "session_scope", scope):
        note = await tm.transposition_note_for_provision(uuid4())
    assert note is not None
    assert "directive" not in note.lower()
    assert "transposes" not in note.lower()
    assert "supplement" in note.lower()
    assert GDPR_CELEX in note


async def test_directive_side_still_uses_transposition_wording():
    """Regression guard: a genuine directive (AIFMD) keeps its transposition
    wording — the regulation branch must not swallow the directive case."""
    scope = _fake_session_scope_returning("32011L0061")
    with patch.object(tm, "session_scope", scope):
        note = await tm.transposition_note_for_provision(uuid4())
    assert note is not None
    assert "directive" in note.lower()
    assert "transposed" in note.lower()
    assert "240/2013" in note


async def test_plain_regulation_without_cz_act_unchanged():
    """A directly-applicable regulation with no supplementing CZ act (MiFIR)
    keeps the original no-transposition wording."""
    scope = _fake_session_scope_returning("32014R0600")
    with patch.object(tm, "session_scope", scope):
        note = await tm.transposition_note_for_provision(uuid4())
    assert note is not None
    assert "directive" not in note.lower()
    assert "directly applicable" in note.lower()
    assert "no" in note.lower()  # "there is no separate Czech transposition"


async def test_unknown_external_id_returns_none():
    scope = _fake_session_scope_returning("99999X9999")
    with patch.object(tm, "session_scope", scope):
        assert await tm.transposition_note_for_provision(uuid4()) is None


async def test_missing_provision_returns_none():
    scope = _fake_session_scope_returning(None)
    with patch.object(tm, "session_scope", scope):
        assert await tm.transposition_note_for_provision(uuid4()) is None
