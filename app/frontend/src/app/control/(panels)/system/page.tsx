"use client";

import { useQuery } from "@tanstack/react-query";
import {
  controlGet,
  fmtBytes,
  fmtPct,
  pressureTone,
  type HistoryResponse,
  type SystemSnapshot,
} from "@/lib/control";
import { useSystemStream } from "@/components/control/useSystemStream";
import {
  AsOf,
  BarGauge,
  MetricTile,
  Panel,
  Sparkline,
  StatusPill,
} from "@/components/control/primitives";
import { LiveLogStream } from "@/components/control/LiveLogStream";

interface NxStatsSystem {
  enabled: boolean;
  reachable?: boolean;
  corpus?: { documents: number; by_hierarchy: { k: string | null; n: number }[] };
  provisions?: { total: number };
  embedding?: { embedded: number; coverage_gap_in_force: number; model: string; dim: number };
  db?: { size_gb: number | null; cache_hit_pct: number | null; top_tables?: { table: string; bytes: number; index_bytes: number }[] };
}

export default function SystemPanel() {
  const sys = useQuery({
    queryKey: ["system"],
    queryFn: () => controlGet<SystemSnapshot>("system"),
    refetchInterval: 5_000,
  });
  const hist = useQuery({
    queryKey: ["system-history"],
    queryFn: () => controlGet<HistoryResponse>("system/history?window=3600"),
    refetchInterval: 10_000,
  });
  const nx = useQuery({
    queryKey: ["nx-stats"],
    queryFn: () => controlGet<NxStatsSystem>("nexus/stats"),
    refetchInterval: 30_000,
  });
  const { events, connected } = useSystemStream({ max: 200 });

  const d = sys.data;
  const r = d?.resources ?? null;
  const col = d?.collector;
  const age = col?.as_of_age_s ?? null;
  const points = hist.data?.points ?? [];
  const series = (k: "cpu_pct" | "mem_pct" | "swap_pct" | "disk_pct") => points.map((p) => p[k]);

  const pressure = r?.pressure ?? "unknown";
  const ptone = pressureTone(pressure);
  const mps = d?.mps ?? r?.mps;
  const groups = d?.memory_segmentation?.groups ?? [];

  const warming = !r && (sys.isLoading || (col?.ticks ?? 0) === 0);

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">System</h1>
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>
            Whole-app resources · Apple-Silicon / MPS · Postgres in Docker
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusPill tone={ptone}>memory {pressure}</StatusPill>
          <AsOf ageS={age} />
        </div>
      </header>

      {warming ? (
        <Panel>
          <div className="py-8 text-center text-[13px]" style={{ color: "var(--ctl-muted)" }}>
            warming up — the collector is taking its first samples…
          </div>
        </Panel>
      ) : (
        <>
          {/* host gauges + sparklines */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Panel title="CPU">
              <BarGauge label="utilisation" pct={r?.cpu?.percent ?? null} tone={(r?.cpu?.percent ?? 0) > 85 ? "critical" : (r?.cpu?.percent ?? 0) > 60 ? "warn" : "ok"} caption={`${r?.cpu?.count ?? "—"} cores · load ${r?.cpu?.load_1m ?? "—"}`} />
              <div className="mt-2"><Sparkline data={series("cpu_pct")} tone="ok" /></div>
            </Panel>
            <Panel title="Memory (unified)">
              <BarGauge label="RAM" pct={r?.memory?.percent ?? null} tone={ptone === "critical" ? "critical" : ptone === "warn" ? "warn" : "ok"} caption={`${fmtBytes(r?.memory?.used_bytes)} / ${fmtBytes(r?.memory?.total_bytes)}`} />
              <div className="mt-2"><Sparkline data={series("mem_pct")} tone="warn" /></div>
            </Panel>
            <Panel title="Swap">
              <BarGauge label="in use" pct={r?.swap?.percent ?? null} tone={(r?.swap?.percent ?? 0) >= 50 ? "critical" : (r?.swap?.percent ?? 0) >= 20 ? "warn" : "ok"} caption={`${fmtBytes(r?.swap?.used_bytes)} / ${fmtBytes(r?.swap?.total_bytes)}`} />
              <div className="mt-2"><Sparkline data={series("swap_pct")} tone="critical" /></div>
            </Panel>
            <Panel title="Disk">
              <BarGauge label={r?.disk?.mount ?? "data volume"} pct={r?.disk?.percent ?? null} tone={(r?.disk?.percent ?? 0) > 90 ? "critical" : (r?.disk?.percent ?? 0) > 80 ? "warn" : "ok"} caption={`${fmtBytes(r?.disk?.free_bytes)} free`} />
              <div className="mt-2"><Sparkline data={series("disk_pct")} tone="ok" /></div>
            </Panel>
          </div>

          {/* secondary tiles */}
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricTile
              label="GPU / MPS"
              value={mps?.available ? fmtBytes(mps.allocated_bytes ?? 0) : "idle"}
              sub={mps?.available ? "allocated (torch live)" : (mps?.reason ?? "no torch process")}
              tone={mps?.available ? "neutral" : "muted"}
            />
            <MetricTile label="Pressure" value={pressure} tone={ptone === "muted" ? "muted" : ptone} sub="RAM is unified with the GPU on this host" />
            <MetricTile label="Collector" value={col?.running ? "running" : "stopped"} tone={col?.running ? "ok" : "critical"} sub={`${col?.ticks ?? 0} ticks · ${col?.collector_errors ?? 0} errors`} ageS={age} />
            <MetricTile label="Tracked footprint" value={fmtBytes((d?.memory_segmentation as { project_total?: { uss_bytes?: number } })?.project_total?.uss_bytes ?? groups.reduce((a, g) => a + g.uss_bytes, 0))} sub={`${groups.length} process groups (USS)`} />
          </div>

          {/* process table + activity */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1.1fr]">
            <Panel title="Processes (by component, USS)" right={<span className="text-[10px]" style={{ color: "var(--ctl-muted)" }}>Postgres = one Docker bucket</span>}>
              <table className="w-full text-[12px]">
                <thead>
                  <tr style={{ color: "var(--ctl-muted)" }}>
                    <th className="py-1 text-left font-normal">component</th>
                    <th className="py-1 text-right font-normal">USS</th>
                    <th className="py-1 text-right font-normal">% RAM</th>
                    <th className="py-1 text-right font-normal">procs</th>
                  </tr>
                </thead>
                <tbody className="tnum">
                  {groups.length === 0 ? (
                    <tr><td colSpan={4} className="py-2" style={{ color: "var(--ctl-muted)" }}>no process data</td></tr>
                  ) : (
                    groups.map((g) => (
                      <tr key={g.key} className="border-t" style={{ borderColor: "#1c1c20" }}>
                        <td className="py-1">{g.label}{!g.uss_exact && <span className="ml-1 text-[10px]" style={{ color: "var(--ctl-muted)" }}>~</span>}</td>
                        <td className="py-1 text-right">{fmtBytes(g.uss_bytes)}</td>
                        <td className="py-1 text-right">{fmtPct(g.percent_of_ram, 1)}</td>
                        <td className="py-1 text-right">{g.n_procs}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </Panel>

            <Panel title="System activity" right={<StatusPill tone={connected ? "ok" : "warn"}>{connected ? "streaming" : "reconnecting"}</StatusPill>}>
              <LiveLogStream events={events} connected={connected} height={300} emptyHint="no system events yet — thresholds emit on change" />
            </Panel>
          </div>

          {/* Databases & Nexus data — "how much storage the DB takes" + the segmentation of all Nexus data */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Panel title="Database storage">
              <MetricTile label="regsuper (app DB)" value={fmtBytes(d?.storage?.data_dir_bytes)} sub="data dir on disk" />
              <div className="mt-2"><MetricTile label="pythia (Nexus DB)" value={nx.data?.db?.size_gb != null ? `${nx.data.db.size_gb} GB` : (nx.data?.enabled ? "—" : "sidecar off")} sub={nx.data?.db?.cache_hit_pct != null ? `cache hit ${nx.data.db.cache_hit_pct}%` : "logical size (Docker = one PG bucket)"} /></div>
              {(nx.data?.db?.top_tables ?? []).slice(0, 4).map((t) => (
                <BarGauge key={t.table} label={t.table} pct={nx.data?.db?.top_tables?.[0]?.bytes ? (100 * t.bytes) / nx.data.db.top_tables[0].bytes : 0} tone="neutral" caption={`${fmtBytes(t.bytes)} (${fmtBytes(t.index_bytes)} idx)`} />
              ))}
            </Panel>
            <Panel title="Nexus data segmentation">
              {nx.data?.enabled === false ? (
                <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>sidecar off — Nexus data unavailable</p>
              ) : (
                <>
                  <p className="mb-2 text-[11px]" style={{ color: "var(--ctl-muted)" }}>{nx.data?.corpus?.documents ?? 0} documents · {(nx.data?.provisions?.total ?? 0).toLocaleString()} provisions</p>
                  {(nx.data?.corpus?.by_hierarchy ?? []).slice(0, 6).map((r) => (
                    <BarGauge key={String(r.k)} label={r.k ?? "—"} pct={nx.data?.corpus?.documents ? (100 * r.n) / nx.data.corpus.documents : 0} tone="neutral" caption={`${r.n}`} />
                  ))}
                </>
              )}
            </Panel>
            <Panel title="Embedding pipeline (bge-m3)">
              {nx.data?.enabled === false ? (
                <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>sidecar off</p>
              ) : (
                <>
                  <BarGauge label="coverage" pct={nx.data?.provisions?.total ? (100 * (nx.data?.embedding?.embedded ?? 0)) / nx.data.provisions.total : 0} tone="ok" caption={`${(nx.data?.embedding?.embedded ?? 0).toLocaleString()} / ${(nx.data?.provisions?.total ?? 0).toLocaleString()} · ${nx.data?.embedding?.dim ?? 1024}-d`} />
                  <p className="mt-2 text-[11px]" style={{ color: (nx.data?.embedding?.coverage_gap_in_force ?? 0) > 0 ? "var(--ctl-warn)" : "var(--ctl-muted)" }}>in-force coverage gap: {nx.data?.embedding?.coverage_gap_in_force ?? 0}</p>
                </>
              )}
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
