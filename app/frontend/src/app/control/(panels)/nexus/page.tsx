"use client";

import { useQuery } from "@tanstack/react-query";
import { controlGet } from "@/lib/control";
import { useSystemStream } from "@/components/control/useSystemStream";
import { BarGauge, MetricTile, Panel, StatusPill } from "@/components/control/primitives";
import { LiveLogStream } from "@/components/control/LiveLogStream";

// MECE: Nexus owns the ingest / graph / embedding / intelligence / gnn / check event classes.
const NEXUS_PREFIXES = ["ingest.", "graph-changed", "refresh.", "caselaw.", "embed.", "materialize.", "intelligence", "gnn.", "check."];

interface Seg { k: string | null; n: number; }
interface Stats {
  enabled: boolean;
  reachable?: boolean;
  error?: string;
  freshness?: { fresh: boolean; live_edge_count: number; staleness_edges: number; last_built_at?: string };
  corpus?: { documents: number; by_hierarchy: Seg[]; by_jurisdiction: Seg[]; by_temporal: Seg[]; case_law_by_court: Seg[] };
  provisions?: { total: number; in_force: number };
  graph?: { edges_total: number; by_type: Seg[]; dangling: number; self_loops: number };
  intelligence?: { n_communities: number; scored: number; isolated_in_force: number; top_hubs: { label: string; ext: string; centrality: number }[] };
  embedding?: { dim: number; model: string; embedded: number; coverage_gap_in_force: number; by_hierarchy: { k: string; total: number; embedded: number }[] };
  gnn?: { present: boolean; checkpoint: string | null; trained_at?: string; stage: number };
  db?: { size_gb: number | null; cache_hit_pct: number | null };
  areas?: { area: string; docs: number; provisions: number }[];
  instruments?: { ext: string; title: string; jurisdiction: string; hierarchy: string; provisions: number }[];
}

// plain-language labels for the legal_domain codes
const AREA_LABEL: Record<string, string> = {
  "financial-market": "Financial markets & services",
  civil: "Civil law & contracts",
  "civil-procedure": "Court & civil procedure",
  criminal: "Criminal law",
  corporate: "Companies & corporate",
  tax: "Tax",
  labour: "Employment & labour",
  administrative: "Administrative law",
  "data-protection": "Data protection (GDPR)",
  aml: "Anti-money-laundering",
  procurement: "Public procurement",
  uncategorized: "Other / untagged",
};
interface NexEvent { kind: string; payload: Record<string, unknown>; created_at: string; }

function SegBars({ rows, total }: { rows: Seg[]; total: number }) {
  return (
    <div className="space-y-1.5">
      {rows.slice(0, 8).map((r) => (
        <BarGauge key={String(r.k)} label={r.k ?? "—"} pct={total ? (100 * r.n) / total : 0} tone="neutral" caption={`${r.n}`} />
      ))}
    </div>
  );
}

export default function NexusPanel() {
  const st = useQuery({ queryKey: ["nx-stats"], queryFn: () => controlGet<Stats>("nexus/stats"), refetchInterval: 15_000 });
  const ev = useQuery({ queryKey: ["nx-events"], queryFn: () => controlGet<{ events: NexEvent[] }>("nexus/events?limit=20"), refetchInterval: 20_000 });
  const { events, connected } = useSystemStream({ max: 200, prefixes: NEXUS_PREFIXES });

  const d = st.data;
  const off = d && !d.enabled;
  const unreachable = d?.enabled && d?.reachable === false;
  const fresh = d?.freshness?.fresh;

  if (off || unreachable) {
    return (
      <div className="space-y-5">
        <header><h1 className="text-lg font-semibold">Nexus</h1></header>
        <Panel><p className="py-6 text-center text-[13px]" style={{ color: off ? "var(--ctl-muted)" : "var(--ctl-critical)" }}>
          {off ? "Nexus is reached via the Pythia sidecar, which is disabled (PYTHIA_ENABLED off)." : `Sidecar unreachable (${d?.error}).`}
        </p></Panel>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Nexus</h1>
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>Knowledge factory · north-star: graph freshness</p>
        </div>
        <StatusPill tone={fresh === undefined ? "muted" : fresh ? "ok" : "critical"}>{fresh === undefined ? "…" : fresh ? "fresh" : "STALE"}</StatusPill>
      </header>

      {/* freshness hero + headline tiles */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <Panel title="Graph freshness (north-star)" className="md:col-span-1">
          <div className="tnum text-3xl font-semibold" style={{ color: fresh ? "var(--ctl-ok)" : "var(--ctl-critical)" }}>{fresh ? "FRESH" : "STALE"}</div>
          <p className="mt-1 text-[11px]" style={{ color: "var(--ctl-muted)" }}>{d?.freshness?.live_edge_count?.toLocaleString()} edges · drift {d?.freshness?.staleness_edges ?? 0}</p>
        </Panel>
        <MetricTile label="communities" value={d?.intelligence?.n_communities ?? "—"} tone={(d?.intelligence?.n_communities ?? 2) <= 1 ? "critical" : "neutral"} />
        <MetricTile label="graph-isolated (in force)" value={(d?.intelligence?.isolated_in_force ?? 0).toLocaleString()} sub="not in regimes/spine" tone="muted" />
        <MetricTile label="GNN" value={d?.gnn?.present ? `stage ${d.gnn.stage}` : "absent"} sub={d?.gnn?.trained_at ?? ""} tone={d?.gnn?.present ? "ok" : "muted"} />
      </div>

      {/* corpus segmentation */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Panel title={`Corpus by hierarchy (${d?.corpus?.documents ?? 0} docs)`}><SegBars rows={d?.corpus?.by_hierarchy ?? []} total={d?.corpus?.documents ?? 0} /></Panel>
        <Panel title="By jurisdiction"><SegBars rows={d?.corpus?.by_jurisdiction ?? []} total={d?.corpus?.documents ?? 0} /></Panel>
        <Panel title="By temporal status"><SegBars rows={d?.corpus?.by_temporal ?? []} total={d?.corpus?.documents ?? 0} /></Panel>
      </div>

      {/* areas of law ingested + largest instruments — what's in the corpus + how big */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Areas of law ingested">
          <div className="space-y-1.5">
            {(d?.areas ?? []).map((a) => (
              <BarGauge
                key={a.area}
                label={AREA_LABEL[a.area] ?? a.area}
                pct={(100 * a.provisions) / (d?.areas?.[0]?.provisions || 1)}
                tone="neutral"
                caption={`${a.provisions.toLocaleString()} provisions · ${a.docs} docs`}
              />
            ))}
          </div>
          <p className="mt-2 text-[10px]" style={{ color: "var(--ctl-muted)" }}>
            provisions counted per area a document is tagged with (areas can overlap).
          </p>
        </Panel>
        <Panel title="Largest instruments (by size)">
          <div className="max-h-[320px] overflow-y-auto">
            <table className="w-full text-[12px]">
              <tbody className="tnum">
                {(d?.instruments ?? []).map((it) => (
                  <tr key={it.ext} className="border-t" style={{ borderColor: "#1c1c20" }}>
                    <td className="py-1 pr-2" style={{ color: "var(--ctl-muted)" }}>{it.jurisdiction}</td>
                    <td className="max-w-[260px] truncate py-1" title={it.title}>{it.title}</td>
                    <td className="py-1 text-right">{it.provisions.toLocaleString()}</td>
                  </tr>
                ))}
                {(d?.instruments?.length ?? 0) === 0 && <tr><td className="py-2" style={{ color: "var(--ctl-muted)" }}>no instruments</td></tr>}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>

      {/* embedding + graph + intelligence */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Panel title="Embedding coverage (bge-m3, 1024-d)">
          <BarGauge label="embedded" pct={d?.provisions?.total ? (100 * (d?.embedding?.embedded ?? 0)) / d.provisions.total : 0} tone="ok" caption={`${(d?.embedding?.embedded ?? 0).toLocaleString()} / ${(d?.provisions?.total ?? 0).toLocaleString()} provisions`} />
          <p className="mt-2 text-[11px]" style={{ color: (d?.embedding?.coverage_gap_in_force ?? 0) > 0 ? "var(--ctl-warn)" : "var(--ctl-muted)" }}>in-force coverage gap: {d?.embedding?.coverage_gap_in_force ?? 0}</p>
        </Panel>
        <Panel title={`Typed graph (${(d?.graph?.edges_total ?? 0).toLocaleString()} edges)`}>
          <SegBars rows={(d?.graph?.by_type ?? []).map((r) => ({ k: r.k, n: r.n }))} total={d?.graph?.edges_total ?? 0} />
          <p className="mt-2 text-[11px]" style={{ color: "var(--ctl-muted)" }}>dangling {d?.graph?.dangling ?? 0} · self-loops {d?.graph?.self_loops ?? 0}</p>
        </Panel>
        <Panel title="Top structural hubs (centrality)">
          <table className="w-full text-[12px]"><tbody className="tnum">
            {(d?.intelligence?.top_hubs ?? []).slice(0, 7).map((h, i) => (
              <tr key={i} className="border-t" style={{ borderColor: "#1c1c20" }}>
                <td className="py-1">{h.label}</td>
                <td className="py-1 text-right" style={{ color: "var(--ctl-muted)" }}>{h.ext}</td>
                <td className="py-1 text-right">{Number(h.centrality ?? 0).toFixed(5)}</td>
              </tr>
            ))}
          </tbody></table>
        </Panel>
      </div>

      {/* ingestion runs + log */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Recent Nexus events (ingest / reindex / checks)">
          <div className="max-h-[260px] overflow-y-auto">
            <table className="w-full text-[12px]"><tbody className="tnum">
              {(ev.data?.events ?? []).map((e, i) => (
                <tr key={i} className="border-t" style={{ borderColor: "#1c1c20" }}>
                  <td className="py-1" style={{ color: "var(--ctl-muted)" }}>{new Date(e.created_at).toLocaleString("en-GB", { hour12: false })}</td>
                  <td className="py-1">{e.kind}</td>
                </tr>
              ))}
              {(ev.data?.events?.length ?? 0) === 0 && <tr><td className="py-2" style={{ color: "var(--ctl-muted)" }}>no events</td></tr>}
            </tbody></table>
          </div>
        </Panel>
        <Panel title="Nexus activity" right={<StatusPill tone={connected ? "ok" : "warn"}>{connected ? "streaming" : "reconnecting"}</StatusPill>}>
          <LiveLogStream events={events} connected={connected} height={240} showModule={false} emptyHint="no live Nexus events (ingest/reindex emit here)" />
        </Panel>
      </div>
    </div>
  );
}
