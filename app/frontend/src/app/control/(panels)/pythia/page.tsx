"use client";

import { useQuery } from "@tanstack/react-query";
import { controlGet } from "@/lib/control";
import { useSystemStream } from "@/components/control/useSystemStream";
import { BarGauge, MetricTile, Panel, StatusPill, type Tone } from "@/components/control/primitives";
import { LiveLogStream } from "@/components/control/LiveLogStream";

// MECE: pythia.* sidecar telemetry, but NOT pythia.call.* (that boundary class is the Polarius panel's).
const PYTHIA_PREFIXES = ["pythia.request", "pythia.health", "pythia.retrieve", "pythia.answer", "pythia.llm", "pythia.latency"];

interface Metrics {
  enabled: boolean;
  reachable?: boolean;
  reason?: string;
  error?: string;
  answer_total?: number;
  gar_pct?: number | null;
  verdict_split?: { ok: number; abstain: number; refuse: number; refuse_pct?: number | null };
  retrieve_total?: number;
  method_mix?: { cte: number; ppr: number; gnn: number };
  gnn_degraded_rate_pct?: number;
  self_repair_rate_pct?: number;
  latency_ms?: Record<string, { p50: number; p95: number; max: number }>;
}
interface Trace {
  kind?: string;
  ts?: string;
  path?: string;
  method?: string;
  verdict?: string | null;
  refused?: boolean;
  confidence?: number;
  n_cites?: number;
  total_ms?: number;
  http_status?: number;
}

export default function PythiaPanel() {
  const m = useQuery({ queryKey: ["py-metrics"], queryFn: () => controlGet<Metrics>("pythia/metrics"), refetchInterval: 8_000 });
  const t = useQuery({ queryKey: ["py-traces"], queryFn: () => controlGet<{ traces: Trace[] }>("pythia/telemetry?limit=40"), refetchInterval: 8_000 });
  const { events, connected } = useSystemStream({ max: 200, prefixes: PYTHIA_PREFIXES });

  const d = m.data;
  const off = d && !d.enabled;
  const unreachable = d?.enabled && d?.reachable === false;
  const gar = d?.gar_pct ?? null;
  const noAnswers = (d?.answer_total ?? 0) === 0;
  const vs = d?.verdict_split;
  const garTone: Tone = off || unreachable || noAnswers || gar === null ? "muted" : gar >= 60 ? "ok" : "warn";

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Pythia</h1>
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>Reasoning sidecar · north-star: grounded-answer rate</p>
        </div>
        <StatusPill tone={off ? "muted" : unreachable ? "critical" : "ok"}>
          {off ? "disabled" : unreachable ? "unreachable" : "connected"}
        </StatusPill>
      </header>

      {off ? (
        <Panel><p className="py-6 text-center text-[13px]" style={{ color: "var(--ctl-muted)" }}>The Pythia sidecar is disabled (PYTHIA_ENABLED is off). Enable it + set PYTHIA_URL to see live telemetry.</p></Panel>
      ) : unreachable ? (
        <Panel><p className="py-6 text-center text-[13px]" style={{ color: "var(--ctl-critical)" }}>Sidecar enabled but unreachable ({d?.error}). Start it: uvicorn pythia.service:app --port 9000.</p></Panel>
      ) : (
        <>
          {/* north-star GAR + verdict split */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1fr]">
            <Panel title="Grounded-answer rate (north-star)">
              <div className="flex items-end gap-3">
                <span className="tnum text-5xl font-semibold" style={{ color: `var(--ctl-${garTone === "muted" ? "muted" : garTone})` }}>
                  {noAnswers || gar === null ? "—" : `${gar.toFixed(0)}%`}
                </span>
                <span className="mb-2 text-[12px]" style={{ color: "var(--ctl-muted)" }}>
                  {noAnswers ? "no answer calls yet" : `of ${d?.answer_total} answers grounded`}
                </span>
              </div>
              {!noAnswers && vs && (
                <div className="mt-3 space-y-2">
                  <BarGauge label="ok (answered)" pct={d?.answer_total ? (100 * vs.ok) / d.answer_total : 0} tone="ok" caption={`${vs.ok}`} />
                  <BarGauge label="abstain (no law on point)" pct={d?.answer_total ? (100 * vs.abstain) / d.answer_total : 0} tone="warn" caption={`${vs.abstain}`} />
                  <BarGauge label="refuse (couldn't ground)" pct={d?.answer_total ? (100 * vs.refuse) / d.answer_total : 0} tone="critical" caption={`${vs.refuse}`} />
                </div>
              )}
              <p className="mt-2 text-[10px]" style={{ color: "var(--ctl-muted)" }}>abstain = honest &quot;no applicable law&quot; (coverage) · refuse = grounded nothing (system signal)</p>
            </Panel>

            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <MetricTile label="retrieves" value={d?.retrieve_total ?? 0} />
                <MetricTile label="gnn degraded" value={`${(d?.gnn_degraded_rate_pct ?? 0).toFixed(0)}%`} tone={(d?.gnn_degraded_rate_pct ?? 0) > 10 ? "critical" : "ok"} />
                <MetricTile label="self-repair" value={`${(d?.self_repair_rate_pct ?? 0).toFixed(0)}%`} tone={(d?.self_repair_rate_pct ?? 0) > 30 ? "warn" : "neutral"} />
              </div>
              <Panel title="Retrieval method mix">
                <div className="flex gap-2">
                  {(["cte", "ppr", "gnn"] as const).map((mm) => (
                    <div key={mm} className="flex-1 rounded-md border p-2 text-center" style={{ borderColor: "#1c1c20", background: "#0c0c0f" }}>
                      <div className="tnum text-lg">{d?.method_mix?.[mm] ?? 0}</div>
                      <div className="text-[10px] uppercase" style={{ color: "var(--ctl-muted)" }}>{mm}</div>
                    </div>
                  ))}
                </div>
              </Panel>
              <Panel title="Latency p50 / p95 (ms)">
                <table className="w-full text-[12px]">
                  <tbody className="tnum">
                    {Object.entries(d?.latency_ms ?? {}).filter(([p]) => p.startsWith("/v1")).map(([path, l]) => (
                      <tr key={path} className="border-t" style={{ borderColor: "#1c1c20" }}>
                        <td className="py-1" style={{ color: "var(--ctl-muted)" }}>{path}</td>
                        <td className="py-1 text-right">{l.p50}</td>
                        <td className="py-1 text-right">{l.p95}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-1 text-[10px]" style={{ color: "var(--ctl-muted)" }}>the answer path is slow by design (88–360s) — not an alarm below 360s.</p>
              </Panel>
            </div>
          </div>

          {/* request inspector */}
          <Panel title="Recent requests" right={<span className="text-[10px]" style={{ color: "var(--ctl-muted)" }}>internal LLM spend lives in the pythia DB — not summed with Polarius call-counts</span>}>
            <table className="w-full text-[12px]">
              <thead><tr style={{ color: "var(--ctl-muted)" }}><th className="text-left font-normal">time</th><th className="text-left font-normal">path</th><th className="text-left font-normal">verdict</th><th className="text-right font-normal">cites</th><th className="text-right font-normal">ms</th><th className="text-right font-normal">status</th></tr></thead>
              <tbody className="tnum">
                {(t.data?.traces ?? []).filter((x) => x.kind !== "request" || x.path?.startsWith("/v1")).slice(0, 18).map((x, i) => (
                  <tr key={i} className="border-t" style={{ borderColor: "#1c1c20" }}>
                    <td className="py-1" style={{ color: "var(--ctl-muted)" }}>{x.ts ? new Date(x.ts).toLocaleTimeString("en-GB", { hour12: false }) : "—"}</td>
                    <td className="py-1">{x.path}{x.method && x.kind === "retrieve" ? ` (${x.method})` : ""}</td>
                    <td className="py-1" style={{ color: x.verdict === "ok" ? "var(--ctl-ok)" : x.verdict === "refuse" ? "var(--ctl-critical)" : x.verdict === "abstain" ? "var(--ctl-warn)" : "var(--ctl-muted)" }}>{x.verdict ?? "—"}</td>
                    <td className="py-1 text-right">{x.n_cites ?? "—"}</td>
                    <td className="py-1 text-right">{x.total_ms ?? "—"}</td>
                    <td className="py-1 text-right" style={{ color: (x.http_status ?? 200) >= 500 ? "var(--ctl-critical)" : "var(--ctl-muted)" }}>{x.http_status ?? "—"}</td>
                  </tr>
                ))}
                {(t.data?.traces?.length ?? 0) === 0 && <tr><td colSpan={6} className="py-2" style={{ color: "var(--ctl-muted)" }}>no requests yet</td></tr>}
              </tbody>
            </table>
          </Panel>

          <Panel title="Pythia activity" right={<StatusPill tone={connected ? "ok" : "warn"}>{connected ? "streaming" : "reconnecting"}</StatusPill>}>
            <LiveLogStream events={events} connected={connected} height={240} showModule={false} emptyHint="no Pythia events yet" />
          </Panel>
        </>
      )}
    </div>
  );
}
