"""Phase B — the law_source router + the Nexus-backed L1 path, fully mocked (no DB, no sidecar).

Pins the logic that the live shadow-compare exercises end-to-end: law_source routes regsuper-vs-nexus;
the project's sector tags map onto Nexus's legal_domain taxonomy; and Nexus candidate rows map onto
L1Candidate while preserving Nexus's centrality order. (asyncio_mode=auto → async tests run directly.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from regsuper.analysis import l1_candidates as L
from regsuper.analysis.l0_intake import ProjectIntake, derive_tags


def _rows(n: int = 3) -> list[dict]:
    return [
        {
            "id": str(uuid4()),
            "label": f"§ {i}",
            "legal_hierarchy": "STATUTE",
            "jurisdiction": "CZ",
            "external_id": "240/2013",
            "source_title": "ZISIF",
        }
        for i in range(n)
    ]


async def test_nexus_path_maps_sector_to_domain_and_rows_to_candidates():
    captured: dict = {}

    async def fake_candidates(
        *, jurisdiction=None, legal_domain=None, legal_hierarchy=None, limit=200
    ):
        captured.update(jurisdiction=jurisdiction, legal_domain=legal_domain, limit=limit)
        return _rows(3)

    async def fake_get_project(_pid):
        return SimpleNamespace(
            jurisdiction_scope=["EU", "CZ"],
            metadata_={"sector_tags": ["investment_funds", "aml", "unknown_tag"]},
        )

    async def fake_emit(*_a, **_k):
        return None

    with (
        patch.object(L, "get_project", fake_get_project),
        patch.object(L.pythia_client, "candidates", fake_candidates),
        patch.object(L, "emit", fake_emit),
    ):
        out = await L._l1_candidates_via_nexus(uuid4(), top_k=10)

    assert captured["jurisdiction"] == ["EU", "CZ"]
    # sector tags → Nexus domains: sorted, deduped, unmapped tag dropped (graceful)
    assert captured["legal_domain"] == ["aml", "financial-market"]
    assert (
        captured["limit"] >= 40
    )  # a larger pool than top_k=10 is fetched for per-doc stratification
    assert len(out) == 3 and all(isinstance(c.provision_id, UUID) for c in out)
    assert (
        out[0].priority_weight > out[1].priority_weight > out[2].priority_weight
    )  # order preserved
    assert out[0].source_external_id == "240/2013"


async def _nexus_domain_for(sector: str, *, entity: str | None = None) -> list[str] | None:
    """Drive the FULL P9 wiring: L0 derive_tags(sector) → project metadata → the Nexus L1's
    sector→legal_domain scope. Returns the legal_domain the sidecar would be asked to filter on."""
    captured: dict = {}

    async def fake_candidates(
        *, jurisdiction=None, legal_domain=None, legal_hierarchy=None, limit=200
    ):
        captured["legal_domain"] = legal_domain
        return _rows(1)

    sector_tags, _ = derive_tags(
        ProjectIntake(name="x", sector=sector, jurisdiction_scope=["CZ"], entity_type=entity)
    )

    async def fake_get_project(_pid):
        return SimpleNamespace(jurisdiction_scope=["CZ"], metadata_={"sector_tags": sector_tags})

    async def fake_emit(*_a, **_k):
        return None

    with (
        patch.object(L, "get_project", fake_get_project),
        patch.object(L.pythia_client, "candidates", fake_candidates),
        patch.object(L, "emit", fake_emit),
    ):
        await L._l1_candidates_via_nexus(uuid4(), top_k=10)
    return captured["legal_domain"]


async def test_p9_new_sectors_route_into_their_nexus_domain():
    # Each new sector must scope L1 INTO the right populated Nexus branch. Some sectors legitimately
    # carry an adjacent substantive branch too (civil-procedure pulls in civil; tax pulls in the
    # financial-market accounting/audit acts) — so assert the required branch is present, not that it
    # is the only one, and that every emitted branch is a real corpus branch.
    async def domains_for(sector):
        d = await _nexus_domain_for(sector)
        assert d is not None and set(d) <= L._NEXUS_DOMAIN_VOCAB, (sector, d)
        return set(d)

    assert "civil" in await domains_for("civil")
    assert "civil-procedure" in await domains_for("civil_procedure")
    assert "administrative" in await domains_for("administrative")
    assert "labour" in await domains_for("labour")
    assert "tax" in await domains_for("tax")
    assert "criminal" in await domains_for("criminal")
    # commercial → the corpus branch is 'corporate' (not 'commercial'); competition rides 'procurement'
    assert "corporate" in await domains_for("commercial")
    assert await domains_for("competition") == {"procurement"}


async def test_p9_unscopable_sectors_never_route_into_an_empty_domain():
    # FOIA/transparency, IP/copyright, cadastre have NO dedicated Nexus branch yet. The wiring must never
    # emit a domain no document carries (that would empty L1, not degrade). Either a real adjacent branch
    # or None (→ jurisdiction scope) is acceptable; a phantom domain is not.
    for sector in ("ip", "social_security", "transparency", "foia"):
        dom = await _nexus_domain_for(sector)
        assert dom is None or set(dom) <= L._NEXUS_DOMAIN_VOCAB, (sector, dom)
    # ip → copyright(unmapped)+civil → civil is real; transparency/foia → no mapped tag → None
    assert await _nexus_domain_for("ip") == ["civil"]
    assert await _nexus_domain_for("transparency") is None
    # social_security carries 'labour' as an adjacent tag → scopes to labour (a real branch), not empty
    assert await _nexus_domain_for("social_security") == ["labour"]


async def test_every_nexus_domain_value_is_a_real_corpus_branch():
    # the import-time assert guards this, but pin it as an explicit regression test too.
    assert set(L._SECTOR_TO_NEXUS_DOMAIN.values()) <= L._NEXUS_DOMAIN_VOCAB


def test_tagging_vocab_mirrors_l0_intake_union():
    # B1a-02 drift-fix: the provision-tagging ALLOWED_* vocab MUST be a superset of every value
    # l0_intake can produce, or project-side tags can't overlap provision-side tags at filter time.
    # SECTOR ← ∪ SECTOR_TO_TAGS.values(); ACTIVITY ← ∪ ENTITY_TO_ACTIVITY.values();
    # ENTITY ← the English keys of ENTITY_TO_ACTIVITY (the two CZ-diacritic AIF aliases are
    # intake-language synonyms, not English provision vocab, so they are not entity tags).
    from regsuper.analysis.l0_intake import ENTITY_TO_ACTIVITY, SECTOR_TO_TAGS
    from regsuper.ingest.tagging import (
        ALLOWED_ACTIVITIES,
        ALLOWED_ENTITIES,
        ALLOWED_SECTORS,
    )

    sector_universe = {t for tags in SECTOR_TO_TAGS.values() for t in tags}
    activity_universe = {t for tags in ENTITY_TO_ACTIVITY.values() for t in tags}
    cz_aliases = {"investicní_fond", "investiční_fond"}
    entity_universe = set(ENTITY_TO_ACTIVITY) - cz_aliases

    assert sector_universe <= set(ALLOWED_SECTORS), sorted(sector_universe - set(ALLOWED_SECTORS))
    assert activity_universe <= set(ALLOWED_ACTIVITIES), sorted(
        activity_universe - set(ALLOWED_ACTIVITIES)
    )
    assert entity_universe <= set(ALLOWED_ENTITIES), sorted(entity_universe - set(ALLOWED_ENTITIES))
    # No duplicates slipped into any tuple.
    for name, tup in (
        ("sectors", ALLOWED_SECTORS),
        ("activities", ALLOWED_ACTIVITIES),
        ("entities", ALLOWED_ENTITIES),
    ):
        assert len(tup) == len(set(tup)), (name, [t for t in tup if list(tup).count(t) > 1])


def test_tagging_finance_lane_unchanged():
    # Drift-fix is additive only: the original finance vocab stays a byte-identical ordered prefix,
    # so finance provisions re-tag to the same tuples (no churn on the served-corpus finance lane).
    from regsuper.ingest.tagging import (
        ALLOWED_ACTIVITIES,
        ALLOWED_ENTITIES,
        ALLOWED_SECTORS,
    )

    orig_sectors = (
        "fin_services",
        "investment_funds",
        "capital_markets",
        "banking",
        "aml",
        "consumer_credit",
        "payments",
        "insurance",
        "real_estate",
        "energy",
        "tech",
        "data_protection",
        "ai_act",
        "healthcare",
        "pharma",
        "medical_devices",
        "manufacturing",
        "product_safety",
        "transport",
        "logistics",
        "retail",
        "consumer_protection",
        "crypto",
        "mica",
        "environmental",
        "utilities",
        "e_money",
        "solvency",
        "construction",
        "leasing",
    )
    orig_activities = (
        "fund_management",
        "marketing",
        "passporting",
        "risk_management",
        "investment_services",
        "client_funds",
        "mifid",
        "best_execution",
        "lending",
        "deposit_taking",
        "capital_requirements",
        "payments",
        "e_money",
        "caspr",
        "data_protection",
        "solvency",
        "fit_and_proper",
        "reporting",
        "disclosure",
        "valuation",
        "remuneration",
        "outsourcing",
        "complaints",
        "consumer_protection",
    )
    orig_entities = (
        "aif",
        "aifm",
        "ucits",
        "investment_company",
        "investment_firm",
        "bank",
        "credit_institution",
        "payment_institution",
        "e_money",
        "fintech",
        "crypto_provider",
        "casp",
        "insurance_undertaking",
        "broker",
        "depositary",
        "auditor",
        "competent_authority",
        "member_state",
        "esma",
        "eba",
        "eiopa",
        "ecb",
    )

    assert ALLOWED_SECTORS[: len(orig_sectors)] == orig_sectors
    assert ALLOWED_ACTIVITIES[: len(orig_activities)] == orig_activities
    assert ALLOWED_ENTITIES[: len(orig_entities)] == orig_entities


async def test_crosswalk_scoped_maps_to_nexus_ids_preserves_order_and_omits_unmapped():
    """B3-06a off-flip scope: run the app's OWN L1, then swap each candidate's id to its NEXUS counterpart via
    the crosswalk. A candidate with no crosswalk row is honestly OMITTED (never substituted); the app L1's
    priority order and the act metadata are preserved; returned ids are NEXUS ids."""
    import contextlib

    from regsuper.analysis.l1_candidates import L1Candidate

    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    n1, n2 = uuid4(), uuid4()  # r3 deliberately has NO crosswalk row

    def _cand(rid, w):
        return L1Candidate(
            provision_id=rid,
            label="§ x",
            legal_hierarchy="cz-act",
            jurisdiction="CZ",
            source_external_id="240/2013",
            source_title="ZISIF",
            priority_weight=w,
        )

    async def fake_rs(_pid, *, top_k=200, as_of=None):
        return [_cand(r1, 100), _cand(r2, 90), _cand(r3, 80)]

    class _Result:
        def all(self):
            return [(r1, n1), (r2, n2)]  # r3 absent → unmapped

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield _Session()

    async def fake_emit(*_a, **_k):
        return None

    with (
        patch.object(L, "_l1_candidates_regsuper", fake_rs),
        patch.object(L, "session_scope", fake_scope),
        patch.object(L, "emit", fake_emit),
    ):
        out = await L.crosswalk_scoped_nexus_candidates(uuid4(), top_k=50)

    assert [c.provision_id for c in out] == [n1, n2]  # NEXUS ids, app-L1 order, r3 omitted
    assert [c.priority_weight for c in out] == [100, 90]  # order/priority preserved
    assert out[0].source_external_id == "240/2013"  # act metadata carried over unchanged


async def test_crosswalk_scoped_empty_when_app_l1_empty_and_skips_db():
    """No app candidates → empty result without touching the crosswalk DB (so an unconfigured project can't
    raise on the lookup)."""

    async def fake_rs(_pid, *, top_k=200, as_of=None):
        return []

    async def fake_emit(*_a, **_k):
        return None

    def _boom():
        raise AssertionError("session_scope must not be called when the app L1 is empty")

    with (
        patch.object(L, "_l1_candidates_regsuper", fake_rs),
        patch.object(L, "session_scope", _boom),
        patch.object(L, "emit", fake_emit),
    ):
        out = await L.crosswalk_scoped_nexus_candidates(uuid4(), top_k=50)
    assert out == []


async def test_law_source_routes_default_regsuper_then_nexus():
    calls = {"regsuper": 0, "nexus": 0}

    async def fake_rs(_pid, *, top_k=200, as_of=None):
        calls["regsuper"] += 1
        return []

    async def fake_nx(_pid, *, top_k=200, as_of=None):
        calls["nexus"] += 1
        return []

    with (
        patch.object(L, "_l1_candidates_regsuper", fake_rs),
        patch.object(L, "_l1_candidates_via_nexus", fake_nx),
    ):
        with patch.object(L, "get_settings", lambda: SimpleNamespace(law_source="regsuper")):
            await L.l1_candidates(uuid4())  # default → the app's own corpus
        with patch.object(L, "get_settings", lambda: SimpleNamespace(law_source="nexus")):
            await L.l1_candidates(uuid4())  # flag on → Nexus
    assert calls == {"regsuper": 1, "nexus": 1}
