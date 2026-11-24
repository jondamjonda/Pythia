"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { controlGet, fmtPct, MODULE_COLOR, normLevel, pressureTone, type ControlEvent, type SystemSnapshot } from "@/lib/control";
import { useSystemStream } from "@/components/control/useSystemStream";
import { MetricTile, Panel, StatusPill } from "@/components/control/primitives";
import { LiveLogStream } from "@/components/control/LiveLogStream";

interface PolNorthStar {
  cascade_grounded_yield_pct: number | null;
  total: number;
  unverified_citation_rate_pct: number;
}
interface PyMetrics {
  enabled: boolean;
  reachable?: boolean;
  gar_pct?: number | null;
  answer_total?: number;
}
interface NxStats {
  enabled: boolean;
  reachable?: boolean;
  freshness?: { fresh: boolean; live_edge_count: number };
}

export default function Overview() {
  const sys = useQuery({
    queryKey: ["system"],
    queryFn: () => controlGet<SystemSnapshot>("system"),
    refetchInterval: 5_000,
  });
  const polNs = useQuery({
    queryKey: ["pol-ns"],
    queryFn: () => controlGet<PolNorthStar>("polarius/north-star"),
    refetchInterval: 15_000,
  });
  const pyM = useQuery({
    queryKey: ["py-metrics"],
    queryFn: () => controlGet<PyMetrics>("pythia/metrics"),
    refetchInterval: 15_000,
  });
  const nxS = useQuery({
    queryKey: ["nx-stats"],
    queryFn: () => controlGet<NxStats>("nexus/stats"),
    refetchInterval: 20_000,
  });
  const alarms = useQuery({
    queryKey: ["alarms"],
    queryFn: () => controlGet<{ alarms: ControlEvent[] }>("alarms?limit=30"),
    refetchInterval: 12_000,
  });
  const { events, connected } = useSystemStream({ max: 200 });
  // merge persisted alarms (survive reload) with live error/warn events from the stream (P3-3)
  const liveAlarms = events.filter((e) => normLevel(e.level) !== "info");
  const allAlarms = [...(alarms.data?.alarms ?? []), ...liveAlarms].slice(-12).reverse();
  const nxFresh = nxS.data?.freshness?.fresh;
  const nxOff = nxS.data && !nxS.data.enabled;
  const nxTone = nxOff || nxFresh === undefined ? "muted" : nxFresh ? "ok" : "critical";
  const pyGar = pyM.data?.gar_pct ?? null;
  const pyOff = pyM.data && !pyM.data.enabled;
  const pyTone = pyOff || pyGar === null ? "muted" : pyGar >= 60 ? "ok" : "warn";
  const d = sys.data;
  const r = d?.resources ?? null;
  const ptone = pressureTone(r?.pressure ?? "unknown");
  const y = polNs.data?.cascade_grounded_yield_pct ?? null;
  const yTone = y === null ? "muted" : y >= 90 ? "ok" : y >= 75 ? "warn" : "critical";

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold">Control room</h1>
        <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>
          At-a-glance health across Nexus, Pythia, and Polarius.
        </p>
      </header>

      {/* module rollups — Nexus + Pythia north-stars wire in Phases 3–4 */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Link href="/control/nexus">
          <Panel key="nexus" title="Nexus" right={<StatusPill tone={nxTone}>{nxOff ? "off" : nxFresh === undefined ? "…" : nxFresh ? "fresh" : "stale"}</StatusPill>}>
            <div className="flex items-baseline gap-2">
              <span className="tnum text-3xl font-semibold" style={{ color: `var(--ctl-${nxTone === "muted" ? "muted" : nxTone})` }}>
                {nxOff ? "off" : nxFresh === undefined ? "—" : nxFresh ? "fresh" : "STALE"}
              </span>
              <span className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>graph freshness</span>
            </div>
            <p className="mt-1 text-[11px]" style={{ color: "var(--ctl-muted)" }}>{nxOff ? "sidecar disabled" : `${(nxS.data?.freshness?.live_edge_count ?? 0).toLocaleString()} edges`}</p>
          </Panel>
        </Link>
        <Link href="/control/pythia">
          <Panel key="pythia" title="Pythia" right={<StatusPill tone={pyTone}>{pyOff ? "off" : pyGar === null ? "idle" : "live"}</StatusPill>}>
            <div className="flex items-baseline gap-2">
              <span className="tnum text-3xl font-semibold" style={{ color: `var(--ctl-${pyTone === "muted" ? "muted" : pyTone})` }}>
                {pyOff ? "off" : pyGar === null ? "—" : `${pyGar.toFixed(0)}%`}
              </span>
              <span className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>grounded-answer rate</span>
            </div>
            <p className="mt-1 text-[11px]" style={{ color: "var(--ctl-muted)" }}>{pyOff ? "sidecar disabled" : `${pyM.data?.answer_total ?? 0} answer calls`}</p>
          </Panel>
        </Link>
        <Link href="/control/polarius">
          <Panel key="polarius" title="Polarius" right={<StatusPill tone={yTone}>{y === null ? "—" : "live"}</StatusPill>}>
            <div className="flex items-baseline gap-2">
              <span className="tnum text-3xl font-semibold" style={{ color: `var(--ctl-${yTone === "muted" ? "muted" : yTone})` }}>
                {y === null ? "—" : `${y.toFixed(0)}%`}
              </span>
              <span className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>grounded-yield</span>
            </div>
            <p className="mt-1 text-[11px]" style={{ color: "var(--ctl-muted)" }}>{polNs.data?.total ?? 0} findings · {(polNs.data?.unverified_citation_rate_pct ?? 0).toFixed(1)}% unverified</p>
          </Panel>
        </Link>
      </div>

      {/* system strip — live now */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <MetricTile label="Memory pressure" value={r?.pressure ?? "…"} tone={ptone === "muted" ? "muted" : ptone} />
        <MetricTile label="CPU" value={fmtPct(r?.cpu?.percent)} tone="neutral" />
        <MetricTile label="RAM" value={fmtPct(r?.memory?.percent)} tone="neutral" />
        <MetricTile label="Disk" value={fmtPct(r?.disk?.percent)} tone="neutral" />
      </div>

      {/* alarms — hydrated from the durable log (survive reload) + live stream (P3-3) */}
      <Panel title="Alarms" right={<StatusPill tone={allAlarms.length ? "warn" : "ok"}>{allAlarms.length ? `${allAlarms.length} recent` : "all clear"}</StatusPill>}>
        {allAlarms.length === 0 ? (
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>no recent warnings or errors</p>
        ) : (
          <div className="space-y-1">
            {allAlarms.map((a, i) => (
              <div key={`${a.timestamp}-${i}`} className="flex items-start gap-2 text-[12px]">
                <span className="tnum" style={{ color: "var(--ctl-muted)" }}>{a.timestamp ? new Date(a.timestamp).toLocaleTimeString("en-GB", { hour12: false }) : "—"}</span>
                <span className="w-16 shrink-0 text-[10px] uppercase" style={{ color: a.module ? MODULE_COLOR[a.module] : "var(--ctl-muted)" }}>{a.module ?? "—"}</span>
                <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: normLevel(a.level) === "error" ? "var(--ctl-critical)" : "var(--ctl-warn)" }} />
                <span className="flex-1" style={{ color: normLevel(a.level) === "error" ? "var(--ctl-critical)" : "var(--ctl-warn)" }}>{a.friendly_message}</span>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Global activity (all modules)"
        right={<StatusPill tone={connected ? "ok" : "warn"}>{connected ? "streaming" : "reconnecting"}</StatusPill>}
      >
        <LiveLogStream events={events} connected={connected} height={320} showModule emptyHint="no control events yet" />
      </Panel>
    </div>
  );
}
