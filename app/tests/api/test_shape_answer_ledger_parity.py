"""WS5 step 48(b) — `_shape_answer` assembled-ledger parity (the ONLY app-repo code in MP-D).

Under the claim-verify ENFORCE posture the engine reassembles the served answer FROM the ledger (demoted
claims excised, gap lines inserted) and stamps the ASSEMBLED ledger into `audit_trace["claim_ledger"]`
(summary + rows). The app's `regsuper.api.answer._shape_answer` is the one MP-D consumer that must handle that
enriched engine dict: it must NOT crash on the extra ledger keys, and it must faithfully MIRROR the
ledger-derived fields the engine already baked into the answer —

  • the reassembled `drafted_answer` / `sections` (the verified claim kept verbatim, the demoted claim's text
    excised) flow through to the shaped sections unchanged;
  • the engine's `refused` / `render_mode` band is mirrored (a served enforce answer stays served; an
    all-core-failed enforce refuse becomes the honest refuse frame with no sections);
  • the `practical` sidecar (the loop's post-reassembly recompute) is shaped when the flag is ON;
  • the AC-CITATION-CHECK still applies on top of the ledger (a chip citing an ungrounded provision is dropped).

These pins guard the app half of the enforce contract against a regression where a new ledger key or a
reassembled-draft shape silently breaks the shaper (crash) or is mis-mirrored (a demoted claim resurfacing, a
refuse being served, or vice-versa).
"""

from __future__ import annotations

from regsuper.api import answer as answer_api


def _assembled_enforce_answer() -> dict:
    """An engine PythiaAnswer dict AS ASSEMBLED BY THE ENFORCE LOOP: the served draft already reflects the
    ledger (the verified obligation kept verbatim; the demoted sanction claim EXCISED + replaced by a gap line),
    and `audit_trace["claim_ledger"]` carries the summary + rows in enforce mode."""
    return {
        "refused": False,
        "verdict": "ok",
        # the REASSEMBLED draft — only the verified claim survives; the demoted claim is a non-citation gap line.
        "drafted_answer": (
            "## Legislativní rámec\n"
            "Depozitář musí oznámit údaje podle § 297 [1].\n"
            "Tuto informaci se nepodařilo ověřit v citovaném ustanovení.\n"
            "## Hodnocení\nOK."
        ),
        "cited_provisions": [{"provision_id": "p-1"}],
        "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 297"}],
        "sections": [
            {
                "kind": "legislative",
                "heading": "Legislativní rámec",
                "text": "Depozitář musí oznámit údaje podle § 297 [1].",
                "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 297"}],
            },
            {"kind": "assessment", "heading": "Hodnocení", "text": "OK.", "citations_map": []},
        ],
        "render_mode": "hedged",  # a demoted core claim forced the band down (VL-E-08 band cap)
        "band": "hedged",
        "meta": {"route": "sound", "intent": "legal", "coverage": "covered"},
        "audit_trace": {
            "metagate_mode": "enforce",
            "claim_ledger": {
                "schema": "cl-v1",
                "summary": {
                    "mode": "enforce",
                    "claims_total": 2,
                    "normative_claims": 2,
                    "demoted": 1,
                    "repaired": 0,
                    "rounds": 1,
                    "per_status": {"verified": 1, "demoted_gap": 1},
                    "loop_budget_exceeded": False,
                    "claim_verify_ms": 42.0,
                },
                "rows": [
                    {
                        "claim_id": "c0",
                        "span": [0, 40],
                        "text": "Depozitář musí oznámit údaje podle § 297 [1].",
                        "slot": "obligations",
                        "type": "rule",
                        "normative": True,
                        "citations": ["p-1"],
                        "final": "verified",
                        "checks": [
                            {
                                "check": "v_cit",
                                "status": "pass",
                                "reason": "cited_and_supported",
                                "evidence": {},
                            }
                        ],
                    },
                    {
                        "claim_id": "c1",
                        "span": [41, 90],
                        "text": "Za porušení hrozí pokuta pět milionů korun [1].",
                        "slot": "obligations",
                        "type": "sanction",
                        "normative": True,
                        "citations": ["p-1"],
                        "final": "demoted_gap",
                        "checks": [
                            {
                                "check": "v_cit",
                                "status": "fail",
                                "reason": "cited_but_unsupported",
                                "evidence": {},
                            }
                        ],
                    },
                ],
            },
        },
    }


def test_shape_answer_does_not_crash_on_assembled_ledger() -> None:
    """The shaper accepts the enforce-assembled dict (extra audit_trace["claim_ledger"] keys) without error and
    returns a coherent served response."""
    out = answer_api._shape_answer(_assembled_enforce_answer(), lang="cs")
    assert out is not None
    assert out.refused is False


def test_shape_answer_mirrors_reassembled_draft_and_band() -> None:
    """The ledger-derived reassembled fields are mirrored faithfully: the verified obligation survives in a
    section, the demoted sanction's text NEVER resurfaces, and the engine's hedged band is carried through."""
    out = answer_api._shape_answer(_assembled_enforce_answer(), lang="cs")
    # served (not refused) + the engine's hedged band mirrored onto the app frame.
    assert out.refused is False
    assert out.render_mode == "hedged" and out.band == "hedged"
    # the verified obligation is present; the demoted sanction's value is absent from every shaped surface.
    section_text = " ".join(s.text for s in out.sections)
    assert "Depozitář musí oznámit údaje podle" in section_text
    assert "pět milionů korun" not in section_text
    # the grounded chip survived AC-CITATION-CHECK; no forged chip leaked.
    assert out.chips and all(c.provision_id == "p-1" for c in out.chips)
    # a hedged (sound) answer carries the hedge banner, never a refusal copy.
    assert out.hedge_banner


def test_shape_answer_refuses_when_ledger_refused_no_core_survived() -> None:
    """An enforce answer the LOOP refused (all core claims demoted ⇒ engine refused=True, render_mode absent)
    is mirrored as the honest refuse frame — refused True, no sections — never served from the stale draft."""
    ans = _assembled_enforce_answer()
    ans["refused"] = True
    ans["verdict"] = "refuse"
    ans["render_mode"] = None
    ans["audit_trace"]["claim_ledger"]["summary"]["per_status"] = {"demoted_gap": 2}
    ans["audit_trace"]["claim_ledger"]["summary"]["demoted"] = 2
    out = answer_api._shape_answer(ans, lang="cs")
    assert out.refused is True
    assert out.render_mode == "refuse"
    assert out.sections == []


def test_shape_answer_shapes_practical_from_assembled_answer() -> None:
    """With answer_practical_block_enabled ON, the loop's post-reassembly practical sidecar is shaped onto the
    served frame and its grounded item survives AC-CITATION-CHECK (ledger-derived, ⊆ the final cited set)."""
    from unittest.mock import patch

    ans = _assembled_enforce_answer()
    ans["practical"] = {
        "items": [
            {
                "slot": "obligations",
                "fact_type": "obligation",
                "value_raw": "oznámit údaje podle § 297",
                "provision_id": "p-1",
                "grounded": True,
            }
        ],
        "gaps": [],
        "demoted_slots": [],
        "profile_used": False,
    }
    settings = answer_api.get_settings().model_copy(update={"answer_practical_block_enabled": True})
    with patch.object(answer_api, "get_settings", lambda: settings):
        out = answer_api._shape_answer(ans, lang="cs")
    assert out.refused is False
    assert out.practical is not None
    assert len(out.practical.items) == 1
    assert out.practical.items[0].provision_id == "p-1"
    # the item's value is one the reassembled answer actually states (ledger-consistent, no phantom fact).
    assert (
        out.practical.items[0].value_raw
        and out.practical.items[0].value_raw in ans["drafted_answer"]
    )
