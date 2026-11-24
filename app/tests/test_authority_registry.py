"""B3 Pillar P3 — competent-authority registry loader + shared vocab.

All DB-free (pure parsers / a fake capturing session). Covers:
  1. authority_vocab: tags match the B1 authority lexicon; normalize_tag maps
     labels/aliases → canonical tags; classify_authority_text on § text;
     area_kind vocabulary is the constrained {okres, orp, kraj}.
  2. Loader parse_seed: normalisation, required-field skip, unknown-type skip,
     idempotent dedup of authorities AND jurisdictions, area_kind constraint.
  3. Loader upsert_seed: authorities upserted before jurisdictions; idempotent
     re-run converges (same rows, no duplicates) using a fake session.
"""

from __future__ import annotations

from regsuper.analysis import authority_vocab as vocab
from regsuper.ingest import authority_registry as reg


# ── 1. Shared vocab ──────────────────────────────────────────────────────────
def test_vocab_has_the_core_b1_authority_types():
    # The SAME tags B1's practical_facts 'authority' extractor emits.
    for tag in ("financni_urad", "ossz", "khs", "oip", "zivnostensky_urad"):
        assert vocab.is_valid_tag(tag), tag
        assert vocab.get(tag) is not None


def test_normalize_tag_passes_through_canonical_tag():
    assert vocab.normalize_tag("financni_urad") == "financni_urad"


def test_normalize_tag_maps_label_and_alias_diacritics_insensitive():
    assert vocab.normalize_tag("finanční úřad") == "financni_urad"
    assert vocab.normalize_tag("FINANCNI URAD") == "financni_urad"  # accent/case-fold
    assert vocab.normalize_tag("OSSZ") == "ossz"
    assert vocab.normalize_tag("krajská hygienická stanice") == "khs"


def test_normalize_tag_unknown_is_none():
    assert vocab.normalize_tag("nebeský úřad") is None
    assert vocab.normalize_tag("") is None
    assert vocab.normalize_tag(None) is None


def test_classify_authority_text_longest_alias_wins_in_sentence():
    # § text mentions the office → the extractor-facing tag.
    txt = "Poplatník podá přiznání místně příslušnému finančnímu úřadu do 1. dubna."
    assert vocab.classify_authority_text(txt) == "financni_urad"
    txt2 = "Zaměstnavatel oznámí úraz oblastnímu inspektorátu práce."
    assert vocab.classify_authority_text(txt2) == "oip"


def test_classify_authority_text_none_when_no_authority():
    assert vocab.classify_authority_text("Text bez jakéhokoli úřadu.") is None
    assert vocab.classify_authority_text(None) is None


def test_area_kinds_are_the_constrained_gazetteer_grains():
    # The loader's area_kind vocabulary == the migration CHECK constraint.
    assert reg._VALID_AREA_KINDS == {"okres", "orp", "kraj"}


# ── 2. Loader parse_seed (pure, DB-free) ─────────────────────────────────────
def _auth(code="FU-JMK", atype="financni_urad", name="FÚ pro JmK", **over):
    base = dict(
        code=code,
        authority_type=atype,
        name=name,
        address="Příkop 25, Brno",
        databox="np48kfp",
        source_uri="https://www.financnisprava.cz/x",
        fetched_at="2026-07-03",
        jurisdictions=[
            {"area_kind": "kraj", "area_code": "CZ064"},
            {"area_kind": "okres", "area_code": "CZ0643"},
        ],
    )
    base.update(over)
    return base


def test_parse_seed_normalises_authority_and_jurisdictions():
    parsed = reg.parse_seed([_auth()])
    assert len(parsed.authorities) == 1
    a = parsed.authorities[0]
    assert set(a.keys()) == set(reg._AUTHORITY_COLUMNS)
    assert a["code"] == "FU-JMK"
    assert a["authority_type"] == "financni_urad"
    assert a["databox"] == "np48kfp"
    assert len(parsed.jurisdictions) == 2
    assert {j["area_kind"] for j in parsed.jurisdictions} == {"kraj", "okres"}
    assert all(j["authority_code"] == "FU-JMK" for j in parsed.jurisdictions)


def test_parse_seed_normalises_authority_type_from_label():
    # A seed row can carry a human label; the loader lands the canonical tag.
    parsed = reg.parse_seed([_auth(atype="finanční úřad")])
    assert parsed.authorities[0]["authority_type"] == "financni_urad"


def test_parse_seed_trims_and_blanks_to_none():
    parsed = reg.parse_seed([_auth(address="   ", databox="  np48kfp  ")])
    a = parsed.authorities[0]
    assert a["address"] is None
    assert a["databox"] == "np48kfp"


def test_parse_seed_skips_rows_missing_required_fields():
    parsed = reg.parse_seed(
        [
            _auth(code=""),  # no code (PK)
            _auth(code="A2", name=""),  # no name (NOT NULL)
            _auth(code="A3", atype="who knows"),  # unknown type → un-joinable, skip
            _auth(code="A4"),  # good
        ]
    )
    assert [a["code"] for a in parsed.authorities] == ["A4"]


def test_parse_seed_drops_jurisdictions_with_bad_area_kind_or_blank_code():
    parsed = reg.parse_seed(
        [
            _auth(
                jurisdictions=[
                    {"area_kind": "obec", "area_code": "500011"},  # not in {okres,orp,kraj}
                    {"area_kind": "orp", "area_code": ""},  # blank area_code
                    {"area_kind": "orp", "area_code": "6208"},  # good
                    "not-a-dict",  # ignored
                ]
            )
        ]
    )
    assert len(parsed.jurisdictions) == 1
    assert parsed.jurisdictions[0] == {
        "authority_code": "FU-JMK",
        "area_kind": "orp",
        "area_code": "6208",
    }


def test_parse_seed_dedups_authorities_on_code_last_wins():
    parsed = reg.parse_seed(
        [
            _auth(code="FU-JMK", name="OLD", jurisdictions=[]),
            _auth(code="FU-JMK", name="FÚ pro JmK", jurisdictions=[]),
        ]
    )
    assert len(parsed.authorities) == 1
    assert parsed.authorities[0]["name"] == "FÚ pro JmK"


def test_parse_seed_dedups_jurisdictions_on_pk():
    parsed = reg.parse_seed(
        [
            _auth(
                jurisdictions=[
                    {"area_kind": "kraj", "area_code": "CZ064"},
                    {"area_kind": "kraj", "area_code": "CZ064"},  # duplicate PK triple
                ]
            )
        ]
    )
    assert len(parsed.jurisdictions) == 1


# ── 3. Loader upsert_seed (fake capturing session) ───────────────────────────
class _FakeSession:
    """Captures (sql, rows) pairs passed to execute(); no real DB."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return None


class _FakeScope:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_upsert_seed_authorities_before_jurisdictions(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(reg, "session_scope", lambda: _FakeScope(session))

    parsed = reg.parse_seed([_auth()])
    n_auth, n_jur = await reg.upsert_seed(parsed)

    assert (n_auth, n_jur) == (1, 2)
    # Two execute() calls, authorities first (FK ordering).
    assert len(session.calls) == 2
    assert "INTO authority " in session.calls[0][0]
    assert "authority_jurisdiction" in session.calls[1][0]


async def test_upsert_seed_is_idempotent_same_rows_each_run(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(reg, "session_scope", lambda: _FakeScope(session))
    parsed = reg.parse_seed([_auth()])

    first = await reg.upsert_seed(parsed)
    second = await reg.upsert_seed(parsed)
    assert first == second == (1, 2)
    # Same authority rows presented both runs (converges, never duplicates).
    auth_rows_run1 = session.calls[0][1]
    auth_rows_run2 = session.calls[2][1]
    assert auth_rows_run1 == auth_rows_run2


async def test_upsert_seed_empty_is_noop(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(reg, "session_scope", lambda: _FakeScope(session))
    result = await reg.upsert_seed(reg.ParsedSeed([], []))
    assert result == (0, 0)
    assert session.calls == []
