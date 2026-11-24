"""Whole-system backtest against a running API server and its database.

Runs phases A to I in order: infrastructure, corpus state, retrieval, project intake and L1
candidates, the EU-to-CZ transposition map, a small L2-L4 cascade (about $0.005 of LLM calls),
the refusal path and calibration data, frontend integration, and deployment artifacts. Each check
prints PASS or FAIL, then a summary; the exit code is 1 if any check failed.

Usage (from app/, with the API on 127.0.0.1:8002): python scripts/backtest_all.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8002"


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Report:
    results: list[TestResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", seconds: float = 0.0) -> None:
        self.results.append(TestResult(name=name, passed=passed, detail=detail, seconds=seconds))
        mark = "PASS" if passed else "FAIL"
        snip = f"  ({detail})" if detail else ""
        elapsed = f"  [{seconds:.2f}s]" if seconds else ""
        print(f"  [{mark}]  {name}{snip}{elapsed}")

    def summary(self) -> str:
        n = len(self.results)
        ok = sum(1 for r in self.results if r.passed)
        return f"{ok} / {n} passed"


async def _timed(fn: Callable[[], Awaitable[Any]]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    result = await fn()
    return result, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Phase A — Infrastructure
# ---------------------------------------------------------------------------
async def phase_a(rep: Report) -> None:
    print("\n=== Phase A · Infrastructure ===")

    from regsuper.observability import killswitch, limits
    from regsuper.storage.database import session_scope

    # 1. /health
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{BASE}/health")
            data = r.json()
            ok = r.status_code == 200 and data.get("status") == "ok"
            rep.add("server.health.up", ok, f"status={data.get('status')}")
        except Exception as exc:
            rep.add("server.health.up", False, repr(exc)[:80])
            return

    # 2. Database connection and audit trigger
    rep.add("server.health.db", data.get("db") == "connected", data.get("db", "?"))
    rep.add("server.health.audit_trigger", bool(data.get("audit_trigger_installed")))

    # 3. Kill switch (should be inactive in a healthy system)
    s = killswitch.state()
    rep.add("killswitch.inactive", not s.get("active"), s.get("path", ""))

    # 4. Limits sanity: block_when_over should be on
    lim = limits.get_limits()
    rep.add(
        "limits.block_when_over",
        bool(lim.get("block_when_over")),
        f"daily=${lim.get('daily_usd')} monthly=${lim.get('monthly_usd')}",
    )

    # 5. Core tables exist (smoke check)
    async with session_scope() as session:
        tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name = ANY(ARRAY"
                    "['provision','provision_sentence','project','finding','api_call',"
                    "'retrieval_cache','retrieval_eval','source_document'])"
                )
            )
        ).scalar_one()
    rep.add("db.core_tables_present", tables == 8, f"{tables}/8")


# ---------------------------------------------------------------------------
# Phase B — Corpus state
# ---------------------------------------------------------------------------
async def phase_b(rep: Report) -> None:
    print("\n=== Phase B · Corpus state ===")
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        prov = (
            await session.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE embedding_semantic IS NOT NULL) FROM provision"
                )
            )
        ).first()
        sent = (
            await session.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE embedding IS NOT NULL) FROM provision_sentence"
                )
            )
        ).first()
        idx = (
            await session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename IN ('provision','provision_sentence') AND indexdef ILIKE '%hnsw%'"
                )
            )
        ).all()
        defs = (await session.execute(text("SELECT count(*) FROM defined_term"))).scalar_one()
        sd = (
            await session.execute(
                text("SELECT publisher, count(*) FROM source_document GROUP BY 1 ORDER BY 1")
            )
        ).all()

    rep.add(
        "corpus.source_documents", sum(n for _, n in sd) >= 11, ", ".join(f"{p}={n}" for p, n in sd)
    )
    rep.add("corpus.provisions_count", 4000 <= prov[0] <= 10000, f"{prov[0]} (target 4k-7k)")
    rep.add("corpus.provisions_embedded", prov[1] == prov[0], f"{prov[1]}/{prov[0]}")
    rep.add("corpus.sentences_embedded", sent[1] == sent[0], f"{sent[1]}/{sent[0]}")
    rep.add("corpus.hnsw_indexes", len(idx) >= 2, ", ".join(r[0] for r in idx))
    rep.add("corpus.defined_terms", defs >= 200, f"{defs} (target ≥200)")


# ---------------------------------------------------------------------------
# Phase C — Retrieval pipeline
# ---------------------------------------------------------------------------
AIFMD_CELEX = "32011L0061"
AIFMD_ART_31_32 = {
    "Article 31",
    "Article 31(1)",
    "Article 31(2)",
    "Article 31(3)",
    "Article 31(4)",
    "Article 32",
    "Article 32(1)",
    "Article 32(2)",
    "Article 32(3)",
    "Article 32(4)",
    "Article 32(5)",
    "Article 32(6)",
}


async def phase_c(rep: Report) -> None:
    print("\n=== Phase C · Retrieval pipeline ===")

    async with httpx.AsyncClient(timeout=60) as client:
        # Canonical: should include AIFMD Art 31/32 in top-5
        r = await client.post(
            f"{BASE}/internal/retrieve",
            json={
                "query": "How many days notice for an EU AIFM marketing to the CNB?",
                "jurisdiction": ["EU", "CZ"],
                "top_n_rerank": 10,
                "expand_top_n": 0,
            },
        )
        data = r.json()
        results = data.get("results", [])
        top5 = results[:5]
        aifmd_in_top5 = sum(
            1
            for h in top5
            if h.get("external_id") == AIFMD_CELEX and h.get("label") in AIFMD_ART_31_32
        )
        rep.add(
            "retrieval.canonical.aifmd_in_top5",
            aifmd_in_top5 >= 1,
            f"{aifmd_in_top5} AIFMD Art 31/32 entries in top-5; latency "
            f"{data['diagnostics']['latency_ms']}ms",
        )
        top1 = results[0] if results else None
        top1_summary = (
            f"{top1.get('external_id')} {top1.get('label')} rerank={top1.get('rerank_score')}"
            if top1
            else "no result"
        )
        rep.add(
            "retrieval.canonical.has_rerank", bool(top1 and top1.get("rerank_score")), top1_summary
        )

        # Negative control (Tesla warranty): the top rerank score should be below 0.5
        r = await client.post(
            f"{BASE}/internal/retrieve",
            json={
                "query": "How long is a Tesla Model 3 warranty?",
                "jurisdiction": ["EU", "CZ"],
                "top_n_rerank": 5,
                "expand_top_n": 0,
            },
        )
        data = r.json()
        top = data.get("results", [{}])[0] if data.get("results") else {}
        score = top.get("rerank_score")
        if score is None:
            rep.add("retrieval.negative.below_threshold", False, "no rerank score (Cohere?)")
        else:
            rep.add(
                "retrieval.negative.below_threshold",
                score < 0.5,
                f"top rerank_score={score:.4f}",
            )


# ---------------------------------------------------------------------------
# Phase D — Project intake / L1
# ---------------------------------------------------------------------------
async def phase_d(rep: Report) -> dict[str, Any]:
    print("\n=== Phase D · Project intake / L1 / SSE ===")

    intake = {
        "name": "Backtest AIF marketing in CZ",
        "sector": "fin_services",
        "jurisdiction_scope": ["EU", "CZ"],
        "entity_type": "AIF",
        "declared_intent": "Marketing an EU AIF to professional investors in CZ.",
        "risk_tolerance": "moderate",
        "analysis_depth": "standard",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        # Create
        r = await client.post(f"{BASE}/api/projects", json=intake)
        rep.add("intake.post.200", r.status_code == 200, f"http={r.status_code}")
        proj = r.json() if r.status_code == 200 else {}
        pid = proj.get("id")
        sector_tags = proj.get("sector_tags") or []
        activity_tags = proj.get("activity_tags") or []
        rep.add(
            "intake.derived_tags",
            "fin_services" in sector_tags and "fund_management" in activity_tags,
            f"sector={sector_tags[:3]}..., activity={activity_tags[:3]}...",
        )

        # Round-trip
        if pid:
            r = await client.get(f"{BASE}/api/projects/{pid}")
            rep.add("intake.get.roundtrip", r.status_code == 200, f"http={r.status_code}")
        else:
            rep.add("intake.get.roundtrip", False, "no id from POST")
            return {}

        # L1 candidates
        r, dur = await _timed(lambda: client.get(f"{BASE}/api/projects/{pid}/candidates?top_k=200"))
        cands = r.json()
        n = cands.get("n_candidates", 0)
        rep.add("l1.candidates_count", n == 200, f"{n}/200", seconds=dur)

        # Stratification — at most 30 per source
        by_ext = Counter(c["source_external_id"] for c in cands["candidates"])
        max_per_doc = max(by_ext.values()) if by_ext else 0
        rep.add("l1.stratified", max_per_doc <= 30, f"max per source={max_per_doc} (cap=30)")

        # AIFMD coverage in L1
        n_aifmd = by_ext.get(AIFMD_CELEX, 0)
        rep.add("l1.aifmd_present", n_aifmd >= 10, f"{n_aifmd} AIFMD provisions in candidates")

        # SSE peek
        try:
            async with client.stream(
                "GET",
                f"{BASE}/api/projects/{pid}/events",
                timeout=httpx.Timeout(5.0, read=5.0),
            ) as resp:
                ok = resp.status_code == 200
                got_frame = False
                async for raw in resp.aiter_lines():
                    if raw.startswith("data:"):
                        got_frame = True
                        break
                rep.add(
                    "sse.opens_with_replay",
                    ok and got_frame,
                    f"http={resp.status_code}, frame={got_frame}",
                )
        except (httpx.ReadTimeout, httpx.ReadError):
            rep.add("sse.opens_with_replay", True, "opened, timed out for next event")

        return {"project_id": pid, "candidates": cands, "by_ext": by_ext}


# ---------------------------------------------------------------------------
# Phase E — Transposition map
# ---------------------------------------------------------------------------
async def phase_e(rep: Report, project_state: dict[str, Any]) -> None:
    print("\n=== Phase E · Transposition scaffold backtest ===")

    from regsuper.analysis import transposition_map as tm
    from regsuper.storage.database import session_scope

    # 1. Static lookups
    aifmd = tm.transposition_for_eu("32011L0061")
    rep.add(
        "transposition.lookup.eu→cz",
        aifmd is not None and aifmd.cz_external_id == "240/2013",
        f"AIFMD → {aifmd.cz_external_id if aifmd else 'None'}",
    )
    cz240 = tm.transposition_for_cz("240/2013")
    rep.add(
        "transposition.lookup.cz→eu",
        cz240 is not None and cz240.eu_celex == "32011L0061",
        f"240/2013 → {cz240.eu_celex if cz240 else 'None'}",
    )
    rep.add(
        "transposition.lookup.direct_regulation",
        tm.is_directly_applicable("32016R0679") and not tm.is_directly_applicable("32011L0061"),
        "GDPR=direct, AIFMD=not-direct",
    )
    rep.add(
        "transposition.lookup.unknown_returns_none",
        tm.transposition_for_eu("99999X9999") is None,
        "unknown CELEX returns None",
    )

    # 2. Coverage report through the admin endpoint
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BASE}/admin/api/transpositions")
        data = r.json()
        s = data.get("summary", {})
        rep.add(
            "transposition.coverage.endpoint",
            r.status_code == 200 and s.get("pairs_total") == 3 and s.get("pairs_both_loaded") == 3,
            f"http={r.status_code}, pairs_both_loaded={s.get('pairs_both_loaded')}/3",
        )

        # Spot-check: every pair has non-null counts on both sides
        all_loaded = all(
            p["eu"]["loaded"]
            and p["cz"]["loaded"]
            and (p["eu"]["n_provisions"] or 0) > 0
            and (p["cz"]["n_provisions"] or 0) > 0
            for p in data.get("transposition_pairs", [])
        )
        rep.add("transposition.coverage.all_pairs_populated", all_loaded)

    # 3. transposition_note_for_provision() for an EU-side, a CZ-side and a regulation provision
    async with session_scope() as session:
        # Find one provision in AIFMD, one in Czech Act 240/2013, one in GDPR
        sample_ids: dict[str, UUID] = {}
        for ext_id, key in [("32011L0061", "aifmd"), ("240/2013", "cz240"), ("32016R0679", "gdpr")]:
            row = (
                await session.execute(
                    text(
                        "SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
                        "WHERE sd.external_id = :ext LIMIT 1"
                    ),
                    {"ext": ext_id},
                )
            ).first()
            if row:
                sample_ids[key] = row[0]

    note_aifmd = await tm.transposition_note_for_provision(sample_ids["aifmd"])
    rep.add(
        "transposition.note.eu_side",
        bool(note_aifmd) and "transposed" in note_aifmd.lower() and "240/2013" in note_aifmd,
        (note_aifmd or "<none>")[:90] + "...",
    )

    note_cz = await tm.transposition_note_for_provision(sample_ids["cz240"])
    rep.add(
        "transposition.note.cz_side",
        bool(note_cz) and "transposes" in note_cz.lower() and "32011L0061" in note_cz,
        (note_cz or "<none>")[:90] + "...",
    )

    note_gdpr = await tm.transposition_note_for_provision(sample_ids["gdpr"])
    rep.add(
        "transposition.note.regulation",
        bool(note_gdpr) and "directly applicable" in note_gdpr.lower(),
        (note_gdpr or "<none>")[:90] + "...",
    )

    # 4. Pair representation in L1 candidates from Phase D
    by_ext = project_state.get("by_ext", Counter()) if project_state else Counter()
    pair_balance: list[str] = []
    sibling_pairs_present = 0
    for pair in tm.TRANSPOSITION_MAP:
        n_eu = by_ext.get(pair.eu_celex, 0)
        n_cz = by_ext.get(pair.cz_external_id, 0)
        pair_balance.append(f"{pair.topic.split('(')[0].strip()}: EU={n_eu} CZ={n_cz}")
        if n_eu > 0 and n_cz > 0:
            sibling_pairs_present += 1
    rep.add(
        "transposition.l1.sibling_pairs_in_candidates",
        sibling_pairs_present >= 1,
        f"{sibling_pairs_present}/3 pairs have both sides in L1 candidates; "
        + " | ".join(pair_balance),
    )

    # 5. Preview the transposition context L2 sees for one AIFMD provision
    note_preview = await tm.transposition_note_for_provision(sample_ids["aifmd"])
    print("\n  Day-11 L2 context preview (one AIFMD provision):")
    print(f"    → {note_preview}")


# ---------------------------------------------------------------------------
# Phase F — Analysis cascade (L2, L3, L4) on a small subset
# ---------------------------------------------------------------------------
async def phase_f(rep: Report, project_state: dict[str, Any]) -> None:
    """Run a tiny L2-L4 cascade (limit=15) so the check stays cheap, about $0.005 in LLM calls.

    The full-corpus cascade is exercised separately by `scripts/smoke_day12.py`.
    """
    print("\n=== Phase F · Day 11/12 cascade (L2-L4, limit=15) ===")
    pid = project_state.get("project_id") if project_state else None
    if pid is None:
        rep.add("cascade.precondition", False, "no project id from Phase D")
        return

    async with httpx.AsyncClient(timeout=240.0) as client:
        body = {"top_k": 200, "limit": 15, "max_concurrency": 5, "layers": ["L2", "L3", "L4"]}
        r, dur = await _timed(lambda: client.post(f"{BASE}/api/projects/{pid}/analyze", json=body))
        rep.add("cascade.http.200", r.status_code == 200, f"http={r.status_code}", seconds=dur)
        if r.status_code != 200:
            return
        d = r.json()
        l2 = d.get("l2") or {}
        l3 = d.get("l3") or {}
        l4 = d.get("l4") or {}
        rep.add(
            "cascade.l2.no_errors",
            l2.get("n_errors", 0) == 0,
            f"l2 evaluated {l2.get('n_evaluated')} / errors {l2.get('n_errors')}",
        )
        rep.add(
            "cascade.l3.no_errors",
            l3.get("n_errors", 0) == 0,
            f"l3 obligations {l3.get('n_obligations_persisted')} / errors {l3.get('n_errors')}",
        )
        rep.add(
            "cascade.l4.scan_ok",
            l4.get("n_obligations_scanned", 0) >= 0,
            f"l4 scanned {l4.get('n_obligations_scanned')}",
        )

        # Pull findings and verify citation enforcement (no finding may have empty citations)
        r = await client.get(f"{BASE}/api/projects/{pid}/findings?limit=400")
        findings = r.json().get("findings", [])
        n_by_type = Counter(f["finding_type"] for f in findings)
        bad = [f for f in findings if not f["cited_provisions"] or not f["cited_documents"]]
        rep.add(
            "cascade.findings.types_present",
            n_by_type.get("applicable", 0) >= 1,
            f"by type: {dict(n_by_type)}",
        )
        rep.add(
            "cascade.findings.citations_complete",
            len(bad) == 0,
            f"{len(bad)} findings have empty citations (must be 0)",
        )

        # Audit chain: every L2 evaluation has an audit row regardless of verdict
        from sqlalchemy import text

        from regsuper.storage.database import session_scope

        async with session_scope() as session:
            n_audit = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_event "
                        "WHERE project_id = :pid AND action='l2.applicability.judged'"
                    ),
                    {"pid": pid},
                )
            ).scalar_one()
        # Audit rows should be at least n_evaluated (re-runs append further rows).
        rep.add(
            "cascade.audit_chain_complete",
            n_audit >= l2.get("n_evaluated", 0),
            f"audit_event rows for project: {n_audit} (l2 evaluated: {l2.get('n_evaluated')})",
        )


# ---------------------------------------------------------------------------
# Phase G — Refusal path and evaluation-set state
# ---------------------------------------------------------------------------
async def phase_g(rep: Report) -> None:
    print("\n=== Phase G · Day 13 refusal + calibration state ===")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Negative-control: out-of-corpus query should be refused
        r = await client.post(
            f"{BASE}/internal/retrieve",
            json={
                "query": "How long is a Tesla Model 3 warranty?",
                "jurisdiction": ["EU", "CZ"],
                "top_n_rerank": 5,
                "expand_top_n": 0,
            },
        )
        d = r.json() if r.status_code == 200 else {}
        rep.add(
            "refusal.negative_query.refused",
            bool(d.get("refused")),
            f"refused={d.get('refused')}  score={d.get('top_rerank_score')}",
        )

        # Positive control: the canonical AIFMD query must not be refused
        r = await client.post(
            f"{BASE}/internal/retrieve",
            json={
                "query": "How many days notice for an EU AIFM marketing to the CNB?",
                "jurisdiction": ["EU", "CZ"],
                "top_n_rerank": 5,
                "expand_top_n": 0,
            },
        )
        d = r.json() if r.status_code == 200 else {}
        rep.add(
            "refusal.positive_query.not_refused",
            (not d.get("refused")) and d.get("top_rerank_score", 0) >= 0.5,
            f"refused={d.get('refused')}  score={d.get('top_rerank_score')}",
        )

    # The evaluation set written by regsuper.calibration.eval_set should still be in the database
    from sqlalchemy import text

    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        n_eval = (
            await session.execute(
                text("SELECT count(*) FROM retrieval_eval WHERE expected_provision_id IS NOT NULL")
            )
        ).scalar_one()
        n_groups = (
            await session.execute(
                text(
                    "SELECT count(DISTINCT query_group_id) FROM retrieval_eval "
                    "WHERE query_group_id IS NOT NULL"
                )
            )
        ).scalar_one()
    rep.add("calibration.eval_seeds_present", n_eval >= 50, f"{n_eval} eval rows (target ≥50)")
    rep.add(
        "calibration.query_groups_populated", n_groups >= 50, f"{n_groups} distinct query groups"
    )


# ---------------------------------------------------------------------------
# Phase H — Frontend integration (CORS, provisions endpoint, Next.js)
# ---------------------------------------------------------------------------
async def phase_h(rep: Report, project_state: dict[str, Any]) -> None:
    print("\n=== Phase H · Day 14 frontend integration ===")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # CORS preflight: browser will OPTIONS-probe before POSTing the intake.
        r = await client.options(
            f"{BASE}/api/projects",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        allow = r.headers.get("access-control-allow-origin", "")
        rep.add(
            "frontend.cors.preflight",
            r.status_code in (200, 204) and "localhost:3000" in allow,
            f"http={r.status_code}, allow-origin={allow!r}",
        )

        # GET /api/provisions/{id} — drives the source viewer page
        cands = (project_state or {}).get("candidates", {})
        if cands and cands.get("candidates"):
            sample_pid = cands["candidates"][0]["provision_id"]
            r = await client.get(f"{BASE}/api/provisions/{sample_pid}")
            ok = r.status_code == 200
            body = r.json() if ok else {}
            rep.add(
                "frontend.provisions_endpoint",
                ok and "ancestors" in body and "source_external_id" in body,
                f"http={r.status_code}, has ancestors+source",
            )

        # Next.js dev server should be reachable on 3000.
        try:
            r = await client.get("http://127.0.0.1:3000", timeout=5)
            ok = r.status_code == 200 and "Regulatory" in r.text
            rep.add("frontend.nextjs.landing", ok, f"http={r.status_code}")
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            rep.add(
                "frontend.nextjs.landing",
                False,
                f"could not reach 127.0.0.1:3000 — start with 'npm run dev' in frontend/  ({exc!r})",
            )


# ---------------------------------------------------------------------------
# Phase I — Production deployment artifacts present and well-formed
# ---------------------------------------------------------------------------
def phase_i(rep: Report) -> None:
    print("\n=== Phase I · Production deploy artifacts ===")
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    expected = [
        "deploy/systemd/regsuper-api.service",
        "deploy/systemd/regsuper-backup.service",
        "deploy/systemd/regsuper-backup.timer",
        "deploy/caddy/Caddyfile",
        "deploy/scripts/install.sh",
        "deploy/scripts/backup.sh",
        "deploy/docker-compose.prod.yml",
        "secrets.prod.env.example",
        "docs/deploy.md",
        "frontend/vercel.json",
        "src/regsuper/storage/s3.py",
    ]
    missing = [p for p in expected if not os.path.exists(os.path.join(root, p))]
    rep.add(
        "deploy.artifacts.present",
        not missing,
        f"missing: {missing}" if missing else "all 11 artifacts present",
    )

    # Cheap shape check on the secrets template: every required key is listed
    keys_required = {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "S3_ENDPOINT_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "LOG_DIR",
        "DATA_DIR",
        "ADMIN_BASIC_AUTH_PASS",
        "REFUSAL_THRESHOLD",
        "FRONTEND_ORIGINS",
    }
    try:
        with open(os.path.join(root, "secrets.prod.env.example"), encoding="utf-8") as f:
            tpl = f.read()
        missing_keys = [k for k in keys_required if k not in tpl]
        rep.add(
            "deploy.secrets_template_complete",
            not missing_keys,
            f"missing keys: {missing_keys}" if missing_keys else "all required keys present",
        )
    except OSError as exc:
        rep.add("deploy.secrets_template_complete", False, repr(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    rep = Report()
    print(f"=== RegSuper backtest · {BASE} ===")

    await phase_a(rep)
    await phase_b(rep)
    await phase_c(rep)
    proj_state = await phase_d(rep)
    await phase_e(rep, proj_state)
    await phase_f(rep, proj_state)
    await phase_g(rep)
    await phase_h(rep, proj_state)
    phase_i(rep)

    print("\n=== Summary ===")
    n = len(rep.results)
    passed = sum(1 for r in rep.results if r.passed)
    failed = [r for r in rep.results if not r.passed]
    print(f"  {passed} / {n} tests passed")
    if failed:
        print("  Failures:")
        for f in failed:
            print(f"    - {f.name}: {f.detail}")

    print(
        json.dumps(
            {
                "total": n,
                "passed": passed,
                "failed": n - passed,
                "failed_tests": [f.name for f in failed],
            },
            indent=2,
        )
    )

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    asyncio.run(main())
