"""PythiaEngine: the retrieval orchestrator.

`PythiaEngine.retrieve()` runs the LLM-free retrieval path end to end: seed retrieval, four-corners
graph expansion (CTE, PPR or GNN), then a ranked list of `CitedProvision`s. Drafting and verifying
an answer happen elsewhere: `pythia.reasoning.answer.answer()` calls `retrieve()` once per
decomposed sub-query and builds the cited, verified answer on top of the results.

Run:  .venv/bin/python -m pythia.engine "query" [cte|ppr]
"""

from __future__ import annotations

import asyncio
import sys

from pythia.config import get_settings
from pythia.contract import CitedProvision, PythiaAnswer, PythiaQuery, TrustTag
from pythia.db import close_pool

# resolve_trust (the pure per-provision trust resolver) and fuse / FusionParams (the fusion
# re-rank) are implemented in the graph package but re-exported through pythia.ports, the single
# door to the graph layer, so the engine never imports a graph submodule directly (a boundary test
# enforces this). resolve_trust reads only columns already on ProvisionRow: no extra database call.
from pythia.ports import FusionParams, NeighborHit, NexusPort, fuse, resolve_trust


class PythiaEngine:
    def __init__(self) -> None:
        self.nexus = NexusPort()  # the single door to the graph layer
        # Cross-encoder rerank of the seeds (bge-reranker-v2-m3, about 2-2.5 GB of memory).
        # PYTHIA_SEED_RERANK_ENABLED=0 turns it off on memory-constrained hosts.
        self.seed_rerank = get_settings().seed_rerank_enabled

    async def retrieve(self, q: PythiaQuery, *, method: str = "cte") -> PythiaAnswer:
        # 1. Seed retrieval. q.rerank_pool widens the reranker's candidate pool; answer() sets it only
        # when seed_pool_scale_enabled is on. When it is None, hybrid_seed uses its built-in pool floor.
        seeds = await self.nexus.search(
            q.query, k=8, jurisdiction=q.jurisdiction, as_of=q.as_of, rerank=self.seed_rerank, rerank_pool=q.rerank_pool
        )
        seed_ids = [s.provision_id for s in seeds]
        if not seed_ids:
            return PythiaAnswer(refused=True, reason="no seed provisions matched the query")
        # 2. Graph expansion. Fail-soft: method="gnn" never raises; an incompatible or empty checkpoint
        # yields finite scores flagged via ExpandResult.degraded / .warnings. cte and ppr never
        # degrade. The seeds are merged on top either way.
        exp = await self.nexus.expand(q.query, seed_ids, method=method, k=q.k, max_hops=q.max_hops, as_of=q.as_of)
        hits = exp.hits
        # 3. Merge. The seeds are direct lexical matches and first-class results, but the expansion
        # returns graph neighbours excluding the seeds, so merge the seeds back on top, dedupe, rank
        # and truncate. Seeds score 1.0-2.0, above typical single-hop graph scores (about 0.3).
        smax = max((s.score for s in seeds), default=1.0) + 1e-9
        # Carry each seed's asof_text (the text of the version in force at a past as_of, filled in by
        # the temporal overlays) onto its rebuilt NeighborHit, so seed provisions keep their
        # past-version text just as graph hits do. It is None unless temporal_substrate_enabled is on
        # and as_of is strictly in the past; _decorate serves it only when as-of rendering is on.
        merged: dict[str, NeighborHit] = {
            s.provision_id: NeighborHit(
                s.provision_id, hops=0, score=1.0 + s.score / smax, relation_path=["seed"], asof_text=s.asof_text
            )
            for s in seeds
        }
        for h in hits:
            merged.setdefault(h.provision_id, h)
        audit: dict = {"seed_scores": [(s.provision_id, round(s.score, 4)) for s in seeds]}
        settings = get_settings()
        # Temporal flags are read once per retrieve, not per provision. Both default to off.
        # temporal_downweight_enabled: resolve_trust refines a provision's temporal status with its
        # own point-in-time status, so a section superseded inside an in-force act is not surfaced.
        downweight = settings.temporal_downweight_enabled
        # temporal_asof_render_enabled: serve a hit's past-version asof_text, when present, instead of
        # the current text.
        render_asof = settings.temporal_asof_render_enabled
        prefetched = None
        # Fusion is decided per request: q.fusion when the caller set it (the answer path does so under
        # answer_path_fusion_enabled), otherwise the global fusion_enabled setting.
        fusion_on = q.fusion if q.fusion is not None else settings.fusion_enabled
        if fusion_on:
            # 4. Fusion re-rank: graph score combined with semantic cosine via RRF, plus a protected
            # floor for rule provisions. Fail-soft: any error falls back to the structural sort (never
            # raises, never empty). The rows are fetched once here so _decorate can reuse them and the
            # protected floor can read analysis_roles.
            try:
                ids = list(merged.keys())
                rows = await self.nexus.fetch_provisions(ids)
                prefetched = {r.id: r for r in rows}
                sem = await self.nexus.score_semantic(q.query, ids)
                roles_by_id = {r.id: (r.analysis_roles or []) for r in rows}
                # The resolved trust is_rule (section-level kind plus per-provision override: the
                # authoritative signal, not just cue-based roles) also floors a provision, so a binding
                # rule in the connected set is never buried. resolve_trust is pure and reuses the rows
                # fetched above. is_rule is decided before the down-weight refinement, so `downweight`
                # does not affect it; it is passed only to keep one consistent flag at this call site.
                is_rule_by_id = {r.id: resolve_trust(r, q.as_of, downweight=downweight).is_rule for r in rows}
                ppr_ids = set(ids) if method == "ppr" else set()
                ranked = fuse(
                    list(merged.values()),
                    sem,
                    roles_by_id,
                    method=method,
                    params=FusionParams(),
                    ppr_ids=ppr_ids,
                    is_rule_by_id=is_rule_by_id,
                )[: q.k]
                audit["fusion"] = {
                    "scorer": "cosine",
                    "params": "default",
                    "per_id": [
                        {"id": h.provision_id, "sem": h.sem_score, "fused": h.fused_score, "why": h.why} for h in ranked
                    ],
                }
            except Exception as exc:  # noqa: BLE001 — fail-soft to structural order
                ranked = sorted(merged.values(), key=lambda h: h.score, reverse=True)[: q.k]
                prefetched = None
                audit["fusion_degraded"] = repr(exc)[:200]
        else:
            ranked = sorted(merged.values(), key=lambda h: h.score, reverse=True)[: q.k]
        # 5. Attach provision metadata and trust tags.
        cited = await self._decorate(
            ranked, as_of=q.as_of, prefetched=prefetched, downweight=downweight, render_asof=render_asof
        )
        # Obligation register: every connected provision whose resolved trust says must_surface (a
        # rule that is temporally active). The answer layer surfaces each of them.
        obligation_register = [c.provision_id for c in cited if c.trust is not None and c.trust.must_surface]
        subgraph: dict = {"method": method, "seeds": seed_ids, "n_expanded": len(hits), "n_returned": len(ranked)}
        if method == "gnn":
            if exp.degraded:
                # The GNN ran on a backbone that was not warm-started, so its scores and trails are not
                # meaningful. Callers must be able to tell this apart from a clean warm load, so it is
                # surfaced rather than failed.
                subgraph["degraded"] = True
            if exp.warnings:
                # Surface load warnings even after a clean warm load (for example a bare checkpoint
                # whose embedder identity could not be verified); never hide them from callers.
                audit["gnn_warnings"] = exp.warnings
        return PythiaAnswer(
            cited_provisions=cited,
            obligation_register=obligation_register,
            reasoning_subgraph=subgraph,
            audit_trace=audit,
        )

    async def _decorate(
        self,
        hits: list[NeighborHit],
        *,
        as_of=None,
        prefetched: dict | None = None,
        downweight: bool = False,
        render_asof: bool = False,
    ) -> list[CitedProvision]:
        # Reuse the rows the fusion step already fetched; without fusion (prefetched is None), fetch
        # them here.
        meta = (
            prefetched
            if prefetched is not None
            else {r.id: r for r in await self.nexus.fetch_provisions([h.provision_id for h in hits])}
        )
        out: list[CitedProvision] = []
        for h in hits:
            m = meta.get(h.provision_id)
            if m is None:
                continue
            # Resolve the trust tag for every decorated provision, seeds and graph-connected ones
            # alike (four_corners, ppr and gnn all end up here). Pure and deterministic.
            rt = resolve_trust(m, as_of, downweight=downweight)
            # As-of rendering: when render_asof is on, serve the hit's past-version asof_text if
            # present, otherwise the current text. getattr guards against hit types without the
            # field, and an empty asof_text falls back to the current text so an empty version row is
            # never served as the answer.
            asof = getattr(h, "asof_text", None)
            served = asof if (render_asof and asof) else (m.text or "")
            out.append(
                CitedProvision(
                    provision_id=h.provision_id,
                    label=m.label,
                    jurisdiction=m.jurisdiction,
                    legal_hierarchy=m.legal_hierarchy,
                    source_title=m.source_title,
                    text_md=served[:240],
                    relation_path=h.relation_path,
                    hops=h.hops,
                    score=h.score,
                    bindingness=m.bindingness,
                    is_authoritative=m.is_authoritative,
                    temporal_status=m.temporal_status,
                    trust=TrustTag(
                        bindingness_tier=rt.bindingness_tier,
                        section_kind=rt.section_kind,
                        temporal_status=rt.temporal_status,
                        is_rule=rt.is_rule,
                        must_surface=rt.must_surface,
                        kind_source="; ".join(rt.reasons) if rt.reasons else None,
                    ),
                    # Fusion confidence; None when fusion did not run (NeighborHit defaults fused_score
                    # and why to None).
                    confidence_retrieval=getattr(h, "fused_score", None),
                    confidence_breakdown=getattr(h, "why", None),
                )
            )
        return out


async def _demo(query: str, method: str) -> None:
    ans = await PythiaEngine().retrieve(PythiaQuery(query=query, k=10), method=method)
    print(f"QUERY: {query}   (method={method})")
    if ans.refused:
        print("REFUSED:", ans.reason)
        await close_pool()
        return
    sg = ans.reasoning_subgraph
    print(f"seeded {len(sg['seeds'])} provisions → {sg['n_returned']} returned ({sg['n_expanded']} graph-expanded)")
    for c in ans.cited_provisions:
        path = " → ".join(c.relation_path) or "(ppr-score)"
        print(
            f"  s={c.score:.4f} h{c.hops} {(c.label or '?'):<14} [{c.jurisdiction}] "
            f"{(c.source_title or '')[:36]}  via {path}"
        )
    await close_pool()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "nabízení alternativního investičního fondu notifikace marketing"
    m = sys.argv[2] if len(sys.argv) > 2 else "cte"
    asyncio.run(_demo(q, m))
