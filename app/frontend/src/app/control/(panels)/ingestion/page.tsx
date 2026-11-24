"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { controlGet, controlPost } from "@/lib/control";
import { useSystemStream } from "@/components/control/useSystemStream";
import { MetricTile, Panel, StatusPill, type Tone } from "@/components/control/primitives";
import { LiveLogStream } from "@/components/control/LiveLogStream";

// MECE: the auto-ingestion panel owns the auto-ingest cycle + the corpus-mutating sub-events it drives
// (refresh/amend, graph-changed drain, gnn-staleness). Nexus owns steady-state knowledge-factory telemetry;
// this panel owns the *autonomous loop* that feeds it. Prefixes per spec §5.2.
const INGEST_PREFIXES = ["auto-ingest", "gnn.", "graph-changed", "refresh."];

// ── shapes (slim mirrors of the honest {enabled, reachable, ...} envelope; only fields the UI reads) ──
interface LastRun {
  run_id?: string;
  started_at?: string | null;
  finished_at?: string | null;
  mode?: string;
  n_discovered?: number;
  n_ingested?: number;
  n_amended?: number;
  n_embedded?: number;
  n_enriched?: number;
  n_errors?: number;
  halted?: boolean;
  halt_reason?: string | null;
}
interface Status {
  enabled: boolean; // app-authoritative
  reachable?: boolean;
  dry_run?: boolean;
  running?: boolean;
  last_run?: LastRun | null;
  next_run_at?: string | null;
  queue_pending?: number;
  coverage_gap_in_force?: number;
  defined_terms_total?: number;
  alarms?: string[];
  reason?: string;
  error?: string;
}
interface RunRow {
  id?: string;
  started_at?: string | null;
  finished_at?: string | null;
  mode?: string;
  n_discovered?: number;
  n_ingested?: number;
  n_amended?: number;
  n_embedded?: number;
  n_enriched?: number;
  n_errors?: number;
  halted?: boolean;
  halt_reason?: string | null;
}
interface History {
  enabled: boolean;
  reachable?: boolean;
  runs?: RunRow[];
  error?: string;
}

// Master switch — three (well, four) confirmed states. Never derived optimistically once a request lands;
// always reconciled to what the server confirms (spec §5.3 — "never a lying switch").
type Master = "ON" | "OFF" | "RUNNING" | "HALTED" | "UNKNOWN";

function masterOf(d: Status | undefined): Master {
  if (!d) return "UNKNOWN";
  // HALTED is a warning state that survives a finished run: a halted last run while still enabled.
  if (d.enabled && d.last_run?.halted && !d.running) return "HALTED";
  if (d.running) return "RUNNING";
  return d.enabled ? "ON" : "OFF";
}

const MASTER_TONE: Record<Master, Tone> = {
  ON: "ok",
  OFF: "muted",
  RUNNING: "ok",
  HALTED: "critical",
  UNKNOWN: "muted",
};

function fmtWhen(ts?: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-GB", { hour12: false });
  } catch {
    return ts;
  }
}

function fmtAgo(ts?: string | null): string {
  if (!ts) return "";
  const t = Date.parse(ts);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${s.toFixed(0)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86_400)}d ago`;
}

// Tiny inline toast (no toast lib in this app; the panel keeps its own ephemeral notice — matches the
// inline-feedback convention used across the control room).
function useToast() {
  const [msg, setMsg] = useState<{ text: string; tone: Tone } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const show = (text: string, tone: Tone) => {
    setMsg({ text, tone });
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setMsg(null), 6_000);
  };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return { msg, show };
}

export default function IngestionPanel() {
  const qc = useQueryClient();
  const { msg, show } = useToast();
  const [dryRunPreview, setDryRunPreview] = useState(true);

  const st = useQuery({
    queryKey: ["ingestion-status"],
    queryFn: () => controlGet<Status>("ingestion/status"),
    refetchInterval: 10_000,
  });
  const hist = useQuery({
    queryKey: ["ingestion-history"],
    queryFn: () => controlGet<History>("ingestion/history?limit=20"),
    refetchInterval: 20_000,
  });
  const { events, connected } = useSystemStream({ max: 200, prefixes: INGEST_PREFIXES });

  const d = st.data;
  const reachable = d?.reachable !== false; // honest envelope: reachable=false when sidecar off/down
  const confirmed = masterOf(d);

  // Optimistic flip: while a flip request is in flight we render the *intended* state, then reconcile.
  const [pending, setPending] = useState<null | "ON" | "OFF">(null);
  const shown: Master = pending ?? confirmed;
  const isOn = shown === "ON" || shown === "RUNNING" || shown === "HALTED";

  const flip = useMutation({
    mutationFn: async (next: boolean) => {
      setPending(next ? "ON" : "OFF");
      return controlPost<{ enabled: boolean; killswitch_engaged?: boolean }>(
        next ? "ingestion/enable" : "ingestion/disable",
        {},
      );
    },
    onSuccess: async (res, next) => {
      // Refetch + reconcile to the CONFIRMED state. The persisted flag is app-authoritative, so the
      // refetched status is the source of truth — never the optimistic guess.
      await qc.invalidateQueries({ queryKey: ["ingestion-status"] });
      if (!next && res?.killswitch_engaged) {
        show("Disabled. A run was in flight — kill-switch engaged to stop it cleanly.", "warn");
      } else {
        show(next ? "Auto-ingestion enabled." : "Auto-ingestion disabled.", "ok");
      }
    },
    onError: (e: unknown) => {
      show(`Flip failed — reverted. ${(e as Error)?.message ?? ""}`, "critical");
    },
    onSettled: () => {
      // Drop the optimistic guess; whatever the refetch confirmed wins.
      setPending(null);
      qc.invalidateQueries({ queryKey: ["ingestion-status"] });
    },
  });

  const runOnce = useMutation({
    mutationFn: (dry: boolean) =>
      controlPost<{ started?: boolean; run_id?: string; reachable?: boolean }>("ingestion/run-once", {
        dry_run: dry,
      }),
    onSuccess: (res, dry) => {
      if (res?.started === false && res?.reachable === false) {
        show("Sidecar unreachable — nothing started.", "warn");
      } else {
        show(dry ? "Dry-run preview started (mutates nothing)." : "Run started.", "ok");
      }
      qc.invalidateQueries({ queryKey: ["ingestion-status"] });
      qc.invalidateQueries({ queryKey: ["ingestion-history"] });
    },
    onError: (e: unknown) => {
      const m = (e as Error)?.message ?? "";
      show(m.includes("409") ? "A run is already in progress." : `Run failed. ${m}`, "critical");
    },
  });

  const stop = useMutation({
    mutationFn: () => controlPost("ingestion/stop", { reason: "operator (auto-ingestion panel)" }),
    onSuccess: () => {
      show("Stop signalled — kill-switch engaged. Current cycle will halt cleanly.", "warn");
      qc.invalidateQueries({ queryKey: ["ingestion-status"] });
      qc.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: unknown) => show(`Stop failed. ${(e as Error)?.message ?? ""}`, "critical"),
  });

  // The toggle is locked while a flip is in flight OR while a cycle is RUNNING (turning OFF mid-run is a
  // separate explicit "Stop" + confirm path — the switch itself never interrupts a run silently).
  const toggleLocked = flip.isPending || shown === "RUNNING";

  const onToggle = () => {
    if (toggleLocked) return;
    // Turning OFF while enabled (or halted) → confirm, because disable also engages the kill-switch if a
    // run is in flight (spec §5.3).
    if (isOn) {
      const ok = window.confirm(
        "Turn auto-ingestion OFF? If a cycle is running this also engages the kill-switch to stop it cleanly.",
      );
      if (!ok) return;
      flip.mutate(false);
    } else {
      flip.mutate(true);
    }
  };

  const lr = d?.last_run ?? null;
  const lrTone: Tone = !lr ? "muted" : lr.halted ? "critical" : (lr.n_errors ?? 0) > 0 ? "warn" : "ok";
  const lrOutcome = !lr ? "no runs yet" : lr.halted ? `HALTED · ${lr.halt_reason ?? "unknown"}` : (lr.n_errors ?? 0) > 0 ? `finished with ${lr.n_errors} error(s)` : "finished clean";
  const alarms = d?.alarms ?? [];

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Auto-ingestion</h1>
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>
            Autonomous legal-corpus updates · default OFF · dry-run first
          </p>
        </div>
        <StatusPill tone={MASTER_TONE[shown]}>
          {shown === "UNKNOWN" ? "…" : shown}
        </StatusPill>
      </header>

      {/* ephemeral inline toast */}
      {msg && (
        <div
          className="rounded-md border px-3 py-2 text-[12px]"
          style={{
            borderColor: "var(--ctl-border)",
            background: "#0c0c0f",
            color: msg.tone === "ok" ? "var(--ctl-ok)" : msg.tone === "warn" ? "var(--ctl-warn)" : "var(--ctl-critical)",
          }}
        >
          {msg.text}
        </div>
      )}

      {/* master toggle + controls */}
      <Panel
        title="Master switch"
        right={
          <span className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>
            {d?.dry_run ? "engine: dry-run mode" : "engine: live"}
          </span>
        }
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-[220px]">
            <p className="text-[13px]" style={{ color: shown === "HALTED" ? "var(--ctl-critical)" : "var(--ctl-muted)" }}>
              {shown === "OFF" && "Auto-ingestion is off. No corpus changes happen automatically."}
              {shown === "ON" && "Auto-ingestion is on. Scheduled cycles will discover and ingest new law."}
              {shown === "RUNNING" && "A cycle is running now. The switch is locked until it finishes — use Stop to interrupt."}
              {shown === "HALTED" && `Last run HALTED${lr?.halt_reason ? ` — ${lr.halt_reason}` : ""}. Still enabled; review before the next cycle.`}
              {shown === "UNKNOWN" && "Loading state…"}
            </p>
            {!reachable && (
              <p className="mt-1 text-[11px]" style={{ color: "var(--ctl-warn)" }}>
                Sidecar unreachable ({d?.reason ?? d?.error ?? "PYTHIA off"}) — the on/off flag is still authoritative and persists.
              </p>
            )}
          </div>

          <div className="flex items-center gap-3">
            <span className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>OFF</span>
            <button
              type="button"
              role="switch"
              aria-checked={isOn}
              aria-label="Auto-ingestion master switch"
              onClick={onToggle}
              disabled={toggleLocked}
              className="relative h-6 w-11 rounded-full transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ background: isOn ? "var(--ctl-ok)" : "#3f3f46" }}
            >
              <span
                className="absolute top-0.5 h-5 w-5 rounded-full transition-[left] duration-200"
                style={{ left: isOn ? "1.375rem" : "0.125rem", background: "#0a0a0a" }}
              />
            </button>
            <span className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>ON</span>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t pt-3" style={{ borderColor: "#1c1c20" }}>
          <label className="flex items-center gap-1.5 text-[12px]" style={{ color: "var(--ctl-muted)" }}>
            <input
              type="checkbox"
              checked={dryRunPreview}
              onChange={(e) => setDryRunPreview(e.target.checked)}
            />
            dry-run preview (mutates nothing)
          </label>
          <button
            onClick={() => runOnce.mutate(dryRunPreview)}
            disabled={runOnce.isPending || shown === "RUNNING"}
            className="rounded-md px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
            style={{ background: "#27272a", color: "var(--ctl-text)" }}
          >
            {runOnce.isPending ? "starting…" : dryRunPreview ? "Run once (preview)" : "Run once now"}
          </button>
          <button
            onClick={() => {
              if (window.confirm("Engage the kill-switch to stop the current cycle? The master switch stays as-is.")) stop.mutate();
            }}
            disabled={stop.isPending}
            className="rounded-md px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
            style={{ background: "var(--ctl-critical)", color: "#0a0a0a" }}
          >
            {stop.isPending ? "stopping…" : "Stop"}
          </button>
        </div>
      </Panel>

      {/* headline metrics */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <MetricTile label="queue pending" value={(d?.queue_pending ?? 0).toLocaleString()} sub="discovered, not yet ingested" tone={(d?.queue_pending ?? 0) > 0 ? "neutral" : "muted"} />
        <MetricTile label="coverage gap (in force)" value={(d?.coverage_gap_in_force ?? 0).toLocaleString()} sub="in-force provisions unembedded" tone={(d?.coverage_gap_in_force ?? 0) > 0 ? "warn" : "ok"} />
        <MetricTile label="defined terms total" value={(d?.defined_terms_total ?? 0).toLocaleString()} sub="H13 stop-the-world watch" tone="neutral" />
        <MetricTile label="next run" value={d?.next_run_at ? fmtAgo(d.next_run_at).replace(" ago", "") : "—"} sub={d?.next_run_at ? fmtWhen(d.next_run_at) : "no schedule"} tone="muted" />
      </div>

      {/* last run + last-cycle summary */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Last run" right={<StatusPill tone={lrTone}>{lr?.halted ? "halted" : (lr?.n_errors ?? 0) > 0 ? "errors" : lr ? "clean" : "—"}</StatusPill>}>
          <div className="text-[13px]" style={{ color: lrTone === "critical" ? "var(--ctl-critical)" : "var(--ctl-text)" }}>{lrOutcome}</div>
          <div className="mt-1 text-[11px]" style={{ color: "var(--ctl-muted)" }}>
            {lr?.mode ? `mode ${lr.mode} · ` : ""}{fmtWhen(lr?.finished_at ?? lr?.started_at)} {lr?.finished_at || lr?.started_at ? `(${fmtAgo(lr?.finished_at ?? lr?.started_at)})` : ""}
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2 text-center">
            {([
              ["discovered", lr?.n_discovered],
              ["ingested", lr?.n_ingested],
              ["amended", lr?.n_amended],
              ["embedded", lr?.n_embedded],
              ["enriched", lr?.n_enriched],
              ["errors", lr?.n_errors],
            ] as const).map(([k, v]) => (
              <div key={k} className="rounded-md border p-2" style={{ borderColor: "#1c1c20", background: "#0c0c0f" }}>
                <div className="tnum text-lg" style={{ color: k === "errors" && (v ?? 0) > 0 ? "var(--ctl-critical)" : "var(--ctl-text)" }}>{v ?? 0}</div>
                <div className="text-[10px] uppercase" style={{ color: "var(--ctl-muted)" }}>{k}</div>
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="Alarms" right={<StatusPill tone={alarms.length ? "critical" : "muted"}>{alarms.length ? `${alarms.length}` : "none"}</StatusPill>}>
          {alarms.length === 0 ? (
            <p className="py-4 text-center text-[12px]" style={{ color: "var(--ctl-muted)" }}>no active alarms</p>
          ) : (
            <ul className="space-y-1.5">
              {alarms.map((a, i) => (
                <li key={i} className="flex items-start gap-2 text-[12px]" style={{ color: "var(--ctl-critical)" }}>
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: "var(--ctl-critical)" }} />
                  <span>{a}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      {/* live activity + history */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Live activity" right={<StatusPill tone={connected ? "ok" : "warn"}>{connected ? "streaming" : "reconnecting"}</StatusPill>}>
          <LiveLogStream events={events} connected={connected} height={300} showModule={false} emptyHint="no live auto-ingest events yet (cycles emit auto-ingest.* / refresh.* here)" />
        </Panel>

        <Panel title="Run history">
          <div className="max-h-[300px] overflow-y-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-left" style={{ color: "var(--ctl-muted)" }}>
                  <th className="py-1 font-normal">when</th>
                  <th className="py-1 font-normal">mode</th>
                  <th className="py-1 text-right font-normal">disc</th>
                  <th className="py-1 text-right font-normal">ing</th>
                  <th className="py-1 text-right font-normal">amd</th>
                  <th className="py-1 text-right font-normal">err</th>
                  <th className="py-1 font-normal">outcome</th>
                </tr>
              </thead>
              <tbody className="tnum">
                {(hist.data?.runs ?? []).map((r, i) => (
                  <tr key={r.id ?? i} className="border-t" style={{ borderColor: "#1c1c20" }}>
                    <td className="py-1 pr-2" style={{ color: "var(--ctl-muted)" }}>{fmtWhen(r.finished_at ?? r.started_at)}</td>
                    <td className="py-1 pr-2">{r.mode ?? "—"}</td>
                    <td className="py-1 text-right">{r.n_discovered ?? 0}</td>
                    <td className="py-1 text-right">{r.n_ingested ?? 0}</td>
                    <td className="py-1 text-right">{r.n_amended ?? 0}</td>
                    <td className="py-1 text-right" style={{ color: (r.n_errors ?? 0) > 0 ? "var(--ctl-critical)" : undefined }}>{r.n_errors ?? 0}</td>
                    <td className="py-1" style={{ color: r.halted ? "var(--ctl-critical)" : "var(--ctl-muted)" }}>
                      {r.halted ? `halted: ${r.halt_reason ?? "?"}` : "ok"}
                    </td>
                  </tr>
                ))}
                {(hist.data?.runs?.length ?? 0) === 0 && (
                  <tr>
                    <td className="py-3" colSpan={7} style={{ color: "var(--ctl-muted)" }}>
                      {hist.data?.reachable === false ? `sidecar unreachable (${hist.data?.error ?? "off"})` : "no runs recorded yet"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}
