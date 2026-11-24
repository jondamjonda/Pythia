"""B3-P2 — soft-law connector SKELETONS (GFŘ / ÚOOÚ / SÚIP): import + cnb-shaped interface, NO fetch."""

from __future__ import annotations

import pytest

from pythia.ingest import gfr_pokyny, suip_guidance, uoou_guidance

_MODULES = (gfr_pokyny, uoou_guidance, suip_guidance)
_FLAGS = {
    gfr_pokyny: "PYTHIA_GFR_CONNECTOR_ENABLED",
    uoou_guidance: "PYTHIA_UOOU_CONNECTOR_ENABLED",
    suip_guidance: "PYTHIA_SUIP_CONNECTOR_ENABLED",
}


@pytest.mark.parametrize("mod", _MODULES)
def test_exposes_cnb_shaped_interface(mod):
    # every skeleton exposes the same cnb_vestnik-shaped surface, without fetching anything
    for name in ("parse_listing", "parse_pdf", "fetch_listing", "ingest_doc", "ingest_sample", "connector_enabled"):
        assert hasattr(mod, name), f"{mod.__name__} missing {name}"
    assert mod.BINDINGNESS == "SOFT_LAW"  # bindingness is ALWAYS soft law
    assert mod.PUBLISHER


@pytest.mark.parametrize("mod", _MODULES)
def test_connector_default_off(mod, monkeypatch):
    monkeypatch.delenv(_FLAGS[mod], raising=False)
    assert mod.connector_enabled() is False
    monkeypatch.setenv(_FLAGS[mod], "1")
    assert mod.connector_enabled() is True


@pytest.mark.parametrize("mod", _MODULES)
@pytest.mark.asyncio
async def test_ingest_sample_inert_while_flag_off(mod, monkeypatch):
    monkeypatch.delenv(_FLAGS[mod], raising=False)
    res = await mod.ingest_sample()
    assert res["mode"] == "DISABLED" and res["flag"] == _FLAGS[mod]


@pytest.mark.parametrize("mod", _MODULES)
@pytest.mark.asyncio
async def test_ingest_sample_skeleton_when_on_does_not_fetch(mod, monkeypatch):
    monkeypatch.setenv(_FLAGS[mod], "1")
    res = await mod.ingest_sample()
    # even ON it is a no-fetch SKELETON preview (no network, no store)
    assert res["mode"].startswith("SKELETON") and res["bindingness"] == "SOFT_LAW"


@pytest.mark.parametrize("mod", _MODULES)
def test_parse_listing_pure_over_fixture_html(mod):
    # a tiny fixture listing → at least one doc parsed, purely (no network)
    html = (
        "<html><body>"
        '<a href="/x/pokyn.pdf">Pokyn GFŘ D-59 k jednotnému postupu</a>'
        '<a href="/x/stan.pdf">Stanovisko č. 3/2024 k předávání údajů</a>'
        '<a href="/x/metod.pdf">Metodický pokyn k inspekci práce</a>'
        '<a href="/x/nope.html">not a pdf</a>'
        "</body></html>"
    ).encode("utf-8")
    docs = mod.parse_listing(html)
    assert isinstance(docs, list)
    # the html anchor is never a PDF → excluded; at least one guidance PDF is picked up
    assert all(d.url.endswith(".pdf") for d in docs)
    assert len(docs) >= 1
