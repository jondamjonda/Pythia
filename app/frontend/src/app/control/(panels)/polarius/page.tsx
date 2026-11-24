"use client";

import { useQuery } from "@tanstack/react-query";
import { controlGet } from "@/lib/control";
import { useSystemStream } from "@/components/control/useSystemStream";
import { BarGauge, MetricTile, Panel, StatusPill, type Tone } from "@/components/control/primitives";
import { LiveLogStream } from "@/components/control/LiveLogStream";

// The Polarius panel is MECE: it streams ONLY module="polarius" events (the cascade/findings/control/
// proxy classes). Build Plan §4.3.
const POLARIUS_PREFIXES = ["analysis.", "retrieval.", "finding.", "killswitch.", "limits.", "pythia.call."];

interface NorthStar {
  by_verdict: Record<string, number>;
  total: number;
  grounded: number;
  cascade_grounded_yield_pct: number | null;
  unverified_citation_rate_pct: number;
}
interface Calls {
  total_calls: number;
  total_errors: number;
  total_cost_usd: number;
  calls_per_min: number;
  by_provider: { provider: string; n: number; cost_usd: number; errors: number }[];
  note: string;
}
interface Run {
  run_id: string;
  project_id: string;
  layers: string[];
  law_source: string;
  started: string;
  ended: string | null;
  status: string;
  counts: { applicable?: number | null; obligations?: number | null; gaps?: number | null };
  failed_layer: string | null;
}
interface Cascades {
  active: number;
  runs: Run[];
}

const LAYERS = ["L2", "L3", "L4"];

function yieldTone(pct: number | null): Tone {
  if (pct === null) return "muted";
  if (pct >= 90) return "ok";
  if (pct >= 75) return "warn";
  return "critical";
}

export default function PolariusPanel() {
  const ns = useQuery({ queryKey: ["pol-ns"], queryFn: () => controlGet<NorthStar>("polarius/north-star"), refetchInterval: 10_000 });
  const calls = useQuery({ queryKey: ["pol-calls"], queryFn: () => controlGet<Calls>("polarius/calls?window=604800"), refetchInterval: 10_000 });
  const casc = useQuery({ queryKey: ["pol-casc"], queryFn: () => controlGet<Cascades>("polarius/cascades"), refetchInterval: 5_000 });
  const { events, connected } = useSystemStream({ max: 200, prefixes: POLARIUS_PREFIXES });

  const y = ns.data?.cascade_grounded_yield_pct ?? null;
  const v = ns.data?.by_verdict ?? {};

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Polarius</h1>
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>App analysis cascade · north-star: cascade grounded-yield</p>
        </div>
        <StatusPill tone={(casc.data?.active ?? 0) > 0 ? "ok" : "muted"}>
          {(casc.data?.active ?? 0) > 0 ? `${casc.data?.active} running` : "idle"}
        </StatusPill>
      </header>

      {/* north-star + citation health */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.1fr_1fr]">
        <Panel title="Cascade grounded-yield (north-star)">
          <div className="flex items-end gap-4">
            <span className="tnum text-5xl font-semibold" style={{ color: `var(--ctl-${yieldTone(y) === "muted" ? "muted" : yieldTone(y) === "ok" ? "ok" : yieldTone(y) === "warn" ? "warn" : "critical"})` }}>
              {y === null ? "—" : `${y.toFixed(0)}%`}
            </span>
            <div className="mb-1 text-[12px]" style={{ color: "var(--ctl-muted)" }}>
              of {ns.data?.total ?? 0} findings carry a trustworthy citation
              <div className="mt-1">unverified rate: <span className="tnum" style={{ color: (ns.data?.unverified_citation_rate_pct ?? 0) > 5 ? "var(--ctl-critical)" : "var(--ctl-text)" }}>{(ns.data?.unverified_citation_rate_pct ?? 0).toFixed(1)}%</span></div>
            </div>
          </div>
          <div className="mt-3 space-y-2">
            {["verified", "fuzzy", "not_found", "empty"].map((k) => (
              <BarGauge
                key={k}
                label={k}
                pct={ns.data?.total ? (100 * (v[k] ?? 0)) / ns.data.total : 0}
                tone={k === "verified" ? "ok" : k === "fuzzy" ? "warn" : "critical"}
                caption={`${v[k] ?? 0} findings`}
              />
            ))}
          </div>
        </Panel>

        <Panel title="External calls Polarius is making (7 days)">
          <div className="grid grid-cols-3 gap-3">
            <MetricTile label="total calls" value={calls.data?.total_calls ?? "—"} sub={`${calls.data?.calls_per_min ?? 0}/min`} />
            <MetricTile label="errors" value={calls.data?.total_errors ?? "—"} tone={(calls.data?.total_errors ?? 0) > 0 ? "warn" : "ok"} />
            <MetricTile label="cost" value={`$${(calls.data?.total_cost_usd ?? 0).toFixed(2)}`} />
          </div>
          <table className="mt-3 w-full text-[12px]">
            <thead><tr style={{ color: "var(--ctl-muted)" }}><th className="text-left font-normal">provider</th><th className="text-right font-normal">calls</th><th className="text-right font-normal">errors</th><th className="text-right font-normal">$</th></tr></thead>
            <tbody className="tnum">
              {(calls.data?.by_provider ?? []).map((p) => (
                <tr key={p.provider} className="border-t" style={{ borderColor: "#1c1c20" }}>
                  <td className="py-1">{p.provider}{p.provider === "pythia" && <span className="ml-1 text-[10px]" style={{ color: "var(--ctl-muted)" }}>boundary</span>}</td>
                  <td className="py-1 text-right">{p.n}</td>
                  <td className="py-1 text-right">{p.errors}</td>
                  <td className="py-1 text-right">${p.cost_usd.toFixed(2)}</td>
                </tr>
              ))}
              {(calls.data?.by_provider?.length ?? 0) === 0 && <tr><td colSpan={4} className="py-2" style={{ color: "var(--ctl-muted)" }}>no calls in window</td></tr>}
            </tbody>
          </table>
          <p className="mt-2 text-[10px]" style={{ color: "var(--ctl-muted)" }}>Pythia = boundary calls only — not summable with the sidecar&apos;s internal LLM spend.</p>
        </Panel>
      </div>

      {/* cascade board */}
      <Panel title="Recent analysis runs" right={<span className="text-[10px]" style={{ color: "var(--ctl-muted)" }}>run-level (per-project detail on the project page)</span>}>
        <div className="space-y-2">
          {(casc.data?.runs ?? []).slice(0, 8).map((r) => (
            <div key={r.run_id} className="flex items-center gap-3 rounded-md border p-2 text-[12px]" style={{ borderColor: "#1c1c20", background: "#0c0c0f" }}>
              <StatusPill tone={r.status === "complete" ? "ok" : r.status === "failed" ? "critical" : "warn"}>{r.status}</StatusPill>
              <span className="tnum" style={{ color: "var(--ctl-muted)" }}>{r.project_id.slice(0, 8)}</span>
              <span className="flex gap-1">
                {LAYERS.map((L) => {
                  const ran = r.layers.includes(L);
                  // Surface WHICH layer failed: tint the specific failed layer's chip critical (the run
                  // status alone doesn't tell the operator where it broke). dead-var fix (L-frontend-06).
                  const failedHere = r.status === "failed" && r.failed_layer === L;
                  return (
                    <span
                      key={L}
                      title={failedHere ? `failed at ${L}` : undefined}
                      className="rounded px-1.5 py-0.5 text-[10px]"
                      style={{
                        background: failedHere ? "rgba(220,38,38,0.18)" : ran ? "#1f1f23" : "transparent",
                        color: failedHere ? "var(--ctl-critical)" : ran ? "var(--ctl-text)" : "#3f3f46",
                      }}
                    >
                      {L}
                    </span>
                  );
                })}
              </span>
              <span className="tnum ml-auto" style={{ color: "var(--ctl-muted)" }}>
                {r.counts.applicable ?? "–"} appl · {r.counts.obligations ?? "–"} obl · {r.counts.gaps ?? "–"} gaps
              </span>
              <span className="text-[10px]" style={{ color: "#52525b" }}>{r.law_source}</span>
            </div>
          ))}
          {(casc.data?.runs?.length ?? 0) === 0 && <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>no analysis runs since the server started — run a project to populate this.</p>}
        </div>
      </Panel>

      <Panel title="Polarius activity" right={<StatusPill tone={connected ? "ok" : "warn"}>{connected ? "streaming" : "reconnecting"}</StatusPill>}>
        <LiveLogStream events={events} connected={connected} height={280} showModule={false} emptyHint="no Polarius events yet — run an analysis" />
      </Panel>
    </div>
  );
}
