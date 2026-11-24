"""N5 Wave 1 — case-law linker: the NO-FABRICATION golden regression.

The linker resolves a ruling's stored cites to interprets/applies edges. The cardinal rule (HARD RULE 1):
an edge is written to a RESOLVED in-corpus provision ONLY — every out-of-corpus or whole-act cite stays a
typed dangling leaf (dst NULL + dst_external). This suite FAILS LOUDLY if an edge is ever invented.

It mixes pure unit tests on the edge builder (synthetic cites) with a DB-backed golden that inserts ONE
synthetic ruling (cites: a real in-corpus §, an out-of-corpus act, a whole-act cite, and an in-corpus act
whose specific § isn't held), runs the linker, and asserts:
  • exactly the in-corpus § resolves → a real edge with a non-NULL dst;
  • everything else stays dangling (dst NULL + dst_external set);
  • every resolved dst's act external_id == the cite's act (the act-scoping guard);
  • interprets vs applies follows the reasoning flag;
  • DOUBLE-EDGE prevention: re-materializing Layer-A does NOT also emit a generic references edge from the
    ruling provision (ruling cites live only in metadata, never cross_reference).
The synthetic ruling is removed in a finally-block so the suite is self-contained and repeatable.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from pythia.db import get_pool
from pythia.ingest import link_caselaw as lk


# ── pure: the edge builder never invents a destination ───────────────────────
def _cite(act, label, *, reasoning, apex=True, anchor="A", raw=""):
    return lk.RuleCite(
        ruling_doc_id="DOC",
        ruling_anchor_pid=anchor,
        act_external_id=act,
        label=label,
        is_reasoning=reasoning,
        is_apex=apex,
        raw_paragraph=raw,
        sections={"oduvodneni"},
    )


def test_build_edges_apex_reasoning_emits_interprets_pair():
    # interprets requires an APEX court (NS/NSS/ÚS) AND a reasoning-section cite
    c = _cite("99/1963", "§ 142", reasoning=True, apex=True)
    edges = lk._build_edges(c, target_pid="TGT", resolved_act="99/1963")
    assert [e.rel_label for e in edges] == ["interprets", "interpreted_by"]
    assert edges[0].src == "A" and edges[0].dst == "TGT" and edges[0].dst_external is None
    assert edges[1].src == "TGT" and edges[1].dst == "A"


def test_build_edges_lower_court_reasoning_is_applies_not_interprets():
    # a LOWER court's reasoning is NOT an authoritative construal (no stare decisis) → applies, never interprets
    c = _cite("99/1963", "§ 142", reasoning=True, apex=False)
    edges = lk._build_edges(c, target_pid="TGT", resolved_act="99/1963")
    assert [e.rel_label for e in edges] == ["applies", "applied_by"]


def test_build_edges_applies_when_not_reasoning_even_for_apex():
    # even an apex court only INTERPRETS from its reasoning; a bare/operative cite is an application
    c = _cite("99/1963", "§ 142", reasoning=False, apex=True)
    edges = lk._build_edges(c, target_pid="TGT", resolved_act="99/1963")
    assert [e.rel_label for e in edges] == ["applies", "applied_by"]


def test_build_edges_out_of_corpus_is_dangling_leaf_only():
    c = _cite("999/9999", "§ 4", reasoning=True)
    edges = lk._build_edges(c, target_pid=None, resolved_act=None)  # unresolved
    assert len(edges) == 1 and edges[0].dst is None
    assert edges[0].dst_external == "999/9999 § 4"  # typed leaf, not invented


def test_build_edges_whole_act_is_dangling_leaf():
    c = _cite("549/1991", None, reasoning=False)
    edges = lk._build_edges(c, target_pid=None, resolved_act=None)
    assert len(edges) == 1 and edges[0].dst is None and edges[0].dst_external == "549/1991"


def test_build_edges_refuses_cross_act_snap():
    # a (corrupt) target whose act != the cite's act MUST NOT be linked — falls back to dangling (HARD RULE 2)
    c = _cite("99/1963", "§ 142", reasoning=True)
    edges = lk._build_edges(c, target_pid="TGT", resolved_act="89/2012")  # WRONG act
    assert len(edges) == 1 and edges[0].dst is None  # refused → dangling, never snapped


# ── DB golden: exactly the in-corpus cite resolves; the rest stay dangling ───
async def _insert_ruling(conn, ecli, cites_meta):
    # an APEX court (Nejvyšší soud) so the interprets path is exercised — a reasoning-section cite from an
    # apex court is an authoritative construal; a lower court would yield 'applies' instead.
    doc_id = await conn.fetchval(
        """INSERT INTO source_document
               (external_id, title, publisher, jurisdiction, legal_hierarchy, document_type, bindingness,
                language, raw_storage_uri, raw_format, raw_checksum, fetched_at, last_checked_at,
                valid_from, status, issuing_authority)
           VALUES ($1,$2,$3,'CZ','CASE_LAW','rozsudek','INTERPRETIVE_CASE_LAW','cs','file:///tmp/test',
                   'test', 'deadbeef', now(), now(), '2024-01-10','ingested',$4)
           RETURNING id""",
        ecli,
        "Rozsudek Nejvyšší soud TEST",
        "Soud:Nejvyšší soud",
        "Nejvyšší soud",
    )
    # výrok (application) + odůvodnění (reasoning) — both carry the same cites, tagged by section flag
    for i, (suffix, is_reasoning) in enumerate((("vyrok", False), ("oduvodneni", True))):
        body = f"body {suffix}"
        await conn.execute(
            """INSERT INTO provision
                   (id, source_document_id, hierarchy_path, ordinal, label, eid, text, text_format,
                    text_hash, language, legal_hierarchy, jurisdiction, metadata)
               VALUES ($1,$2,$3::ltree,$4,$5,$6,$7,'plain',$8,'cs','CASE_LAW','CZ',$9::jsonb)""",
            uuid4(),
            doc_id,
            f"ruling_{suffix}",
            i,
            suffix.capitalize(),
            f"ruling_{suffix}",
            body,
            hashlib.sha256(body.encode()).hexdigest(),
            json.dumps({"section": suffix, "is_reasoning": is_reasoning, "cites": cites_meta}, ensure_ascii=False),
        )
    return doc_id


@pytest.mark.live
async def test_db_golden_no_fabrication_split():
    pool = await get_pool()
    ecli = "ECLI:CZ:TEST:9999:LINKER.GOLDEN"
    cites_meta = [
        {"act": "99/1963", "label": "§ 142", "lex_type": "PREDPIS_ZAKON", "raw_paragraph": "142"},  # in-corpus
        {"act": "999/9999", "label": "§ 4", "lex_type": "PREDPIS_ZAKON", "raw_paragraph": "4"},  # out-of-corpus act
        {
            "act": "549/1991",
            "label": None,
            "lex_type": "PREDPIS_ZAKON",
            "raw_paragraph": "",
        },  # whole-act (in-corpus act)
        {
            "act": "99/1963",
            "label": "§ 99999z",
            "lex_type": "PREDPIS_ZAKON",
            "raw_paragraph": "99999z",
        },  # in-corpus act, § not held
    ]
    # guard: 99/1963 § 142 is really in-corpus; 999/9999 really is NOT
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM source_document WHERE external_id='99/1963'") == 1
        assert await conn.fetchval("SELECT count(*) FROM source_document WHERE external_id='999/9999'") == 0
        tgt142 = await conn.fetchval(
            "SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
            "WHERE sd.external_id='99/1963' AND p.label='§ 142'"
        )
        assert tgt142 is not None
        # clean any prior run
        await conn.execute(
            "DELETE FROM provision WHERE source_document_id IN (SELECT id FROM source_document WHERE external_id=$1)",
            ecli,
        )
        await conn.execute("DELETE FROM source_document WHERE external_id=$1", ecli)
        doc_id = await _insert_ruling(conn, ecli, cites_meta)

    try:
        await lk.link_caselaw(dry_run=False)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT src, dst, dst_external, edge_type, rel_id FROM pythia_edge "
                "WHERE source='case_law' AND (src IN (SELECT id FROM provision WHERE source_document_id=$1) "
                "   OR dst IN (SELECT id FROM provision WHERE source_document_id=$1))",
                doc_id,
            )
            anchor = await conn.fetchval(
                "SELECT id FROM provision WHERE source_document_id=$1 ORDER BY ordinal LIMIT 1", doc_id
            )

            resolved_fwd = [r for r in rows if r["edge_type"] in ("interprets", "applies") and r["dst"] is not None]
            dangling = [r for r in rows if r["edge_type"] in ("interprets", "applies") and r["dst"] is None]

            # EXACTLY ONE resolved forward edge — the in-corpus § 142 (the cite present in BOTH sections, so
            # the OR-of-reasoning makes it interprets). The other three cites are all dangling.
            assert len(resolved_fwd) == 1, f"expected 1 resolved edge, got {[dict(r) for r in resolved_fwd]}"
            res = resolved_fwd[0]
            assert res["edge_type"] == "interprets"  # apex court + odůvodnění → construal
            assert res["dst"] == tgt142  # the EXACT in-corpus provision
            assert res["dst_external"] is None
            assert res["src"] == anchor

            # the resolved dst's act external_id == the cite's act (act-scoping guard, on the live row)
            dst_act = await conn.fetchval(
                "SELECT sd.external_id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
                "WHERE p.id=$1",
                res["dst"],
            )
            assert dst_act == "99/1963"

            # the inverse interpreted_by edge exists (target → ruling)
            inv = await conn.fetchval(
                "SELECT count(*) FROM pythia_edge WHERE source='case_law' AND edge_type='interpreted_by' "
                "AND src=$1 AND dst=$2",
                tgt142,
                anchor,
            )
            assert inv == 1

            # NO FABRICATION: the three non-in-corpus-§ cites are ALL dangling (dst NULL + dst_external set),
            # and NONE of them is linked to ANY provision.
            dangling_ext = sorted(r["dst_external"] for r in dangling)
            assert dangling_ext == ["549/1991", "99/1963 § 99999z", "999/9999 § 4"], dangling_ext
            assert all(r["dst"] is None and r["dst_external"] for r in dangling)
            # the out-of-corpus act 999/9999 must NEVER appear as a resolved dst anywhere
            assert not any(r["dst"] is not None and "999/9999" in (r["dst_external"] or "") for r in rows)

            # DOUBLE-EDGE PREVENTION: re-materialize Layer-A and confirm the ruling provision produced NO
            # generic references edge (a ruling cite must be interprets/applies ONLY, never references).
        from pythia.graph.materialize import materialize_layer_a

        await materialize_layer_a()
        async with pool.acquire() as conn:
            generic = await conn.fetchval(
                "SELECT count(*) FROM pythia_edge WHERE source<>'case_law' AND edge_type IN "
                "('references','referenced_by','internal_reference') "
                "AND src IN (SELECT id FROM provision WHERE source_document_id=$1)",
                doc_id,
            )
            assert generic == 0, "a ruling cite leaked into a generic references edge (double-edge)"
            # and the case-law edges survive the materialize scoped-delete (source='case_law' is preserved)
            survived = await conn.fetchval(
                "SELECT count(*) FROM pythia_edge WHERE source='case_law' "
                "AND src IN (SELECT id FROM provision WHERE source_document_id=$1)",
                doc_id,
            )
            assert survived >= 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM pythia_edge WHERE source='case_law' AND (src IN "
                "(SELECT id FROM provision WHERE source_document_id=$1) OR dst IN "
                "(SELECT id FROM provision WHERE source_document_id=$1))",
                doc_id,
            )
            await conn.execute("DELETE FROM provision WHERE source_document_id=$1", doc_id)
            await conn.execute("DELETE FROM source_document WHERE id=$1", doc_id)
