"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { controlGet, controlPost } from "@/lib/control";
import { BarGauge, Panel, StatusPill, type Tone } from "@/components/control/primitives";

// ── shapes ──
interface KeyRow {
  key_id: string;
  label: string;
  provider: string;
  spent_usd: number;
  cap_usd: number;
  pct: number;
  tier: string;
  legacy: boolean;
}
interface Weekly {
  week_start: string;
  keys: KeyRow[];
}
interface Ceiling {
  key: string;
  label: string;
  current_pct?: number | null;
  ceiling_pct?: number | null;
  current_gb?: number | null;
  ceiling_gb?: number | null;
  floor_gb?: number | null;
  available?: boolean | null;
  unit: string;
  action: string;
  note?: string | null;
}
interface Resources {
  watchdog_enabled: boolean;
  ceilings: Ceiling[];
  kill_switch: { active?: boolean; reason?: string | null };
}

const TIER_TONE: Record<string, Tone> = {
  ok: "ok",
  uncapped: "muted",
  warn: "warn",
  soft: "critical",
  hard: "critical",
};

export default function ApiCapsPanel() {
  const qc = useQueryClient();
  const weekly = useQuery({ queryKey: ["weekly"], queryFn: () => controlGet<Weekly>("weekly"), refetchInterval: 8_000 });
  const res = useQuery({ queryKey: ["resources"], queryFn: () => controlGet<Resources>("resources"), refetchInterval: 5_000 });

  const worstTier =
    weekly.data?.keys.reduce<string>((w, k) => {
      const order = ["ok", "uncapped", "warn", "soft", "hard"];
      return order.indexOf(k.tier) > order.indexOf(w) ? k.tier : w;
    }, "ok") ?? "ok";
  const ks = res.data?.kill_switch ?? {};

  const killMut = useMutation({
    mutationFn: (engage: boolean) =>
      controlPost(engage ? "killswitch/engage" : "killswitch/release", { reason: "operator (control panel)" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["resources"] });
      qc.invalidateQueries({ queryKey: ["system"] });
    },
  });

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold">API & caps</h1>
        <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>
          Per-key weekly budgets · hard CPU/GPU/storage ceilings · the master kill-switch.
        </p>
      </header>

      {/* safety banner */}
      <Panel
        className="!p-4"
        title="Safety"
        right={
          <StatusPill tone={ks.active ? "critical" : (TIER_TONE[worstTier] ?? "ok")}>
            {ks.active ? "STOPPED" : worstTier === "ok" ? "all clear" : `worst: ${worstTier}`}
          </StatusPill>
        }
      >
        <div className="flex items-center justify-between gap-4">
          <p className="text-[13px]" style={{ color: ks.active ? "var(--ctl-critical)" : "var(--ctl-muted)" }}>
            {ks.active
              ? `Everything is stopped — ${ks.reason ?? "operator action"}. No external calls or new work will run.`
              : "External calls and new analysis are running normally. Caps will pause work before anything breaks."}
          </p>
          <button
            onClick={() => killMut.mutate(!ks.active)}
            disabled={killMut.isPending}
            className="shrink-0 rounded-md px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
            style={{
              background: ks.active ? "#27272a" : "var(--ctl-critical)",
              color: ks.active ? "var(--ctl-text)" : "#0a0a0a",
            }}
          >
            {ks.active ? "Resume" : "Stop everything"}
          </button>
        </div>
      </Panel>

      {/* weekly budgets */}
      <Panel title={`Weekly budgets (per API key) — week of ${weekly.data?.week_start?.slice(0, 10) ?? "…"}`}>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {(weekly.data?.keys ?? []).map((k) => (
            <KeyBudgetCard key={k.key_id} row={k} onSaved={() => qc.invalidateQueries({ queryKey: ["weekly"] })} />
          ))}
          {weekly.data && weekly.data.keys.length === 0 && (
            <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>no API keys tracked yet</p>
          )}
        </div>
      </Panel>

      {/* resource ceilings */}
      <Panel
        title="Resource ceilings (hard caps)"
        right={
          <button
            onClick={async () => {
              await controlPost("watchdog/toggle", { enabled: !res.data?.watchdog_enabled });
              qc.invalidateQueries({ queryKey: ["resources"] });
            }}
            className="rounded-md px-2 py-1 text-[11px]"
            style={{ background: "#27272a", color: "var(--ctl-text)" }}
          >
            watchdog: {res.data?.watchdog_enabled ? "on" : "off"}
          </button>
        }
      >
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {(res.data?.ceilings ?? []).map((c) => (
            <CeilingRow key={c.key} c={c} onSaved={() => qc.invalidateQueries({ queryKey: ["resources"] })} />
          ))}
        </div>
        <p className="mt-3 text-[11px]" style={{ color: "var(--ctl-muted)" }}>
          macOS has no OS-level quotas — caps work by refusing to start risky work and, if a breach
          persists, tripping the kill-switch. GPU/MPS is enforced only while a GNN job is actually running.
        </p>
      </Panel>
    </div>
  );
}

function KeyBudgetCard({ row, onSaved }: { row: KeyRow; onSaved: () => void }) {
  const [cap, setCap] = useState(row.cap_usd ? String(row.cap_usd) : "");
  const save = useMutation({
    mutationFn: () => controlPost("limits", { weekly_key_caps_usd: { [row.key_id]: Number(cap) || 0 } }),
    onSuccess: onSaved,
  });
  const tone = TIER_TONE[row.tier] ?? "muted";
  return (
    <div className="rounded-lg border p-3" style={{ background: "#0c0c0f", borderColor: "var(--ctl-border)" }}>
      <div className="flex items-center justify-between">
        <span className="truncate text-[13px]" title={row.key_id}>
          {row.label}
        </span>
        <StatusPill tone={tone}>{row.cap_usd > 0 ? row.tier : "no cap"}</StatusPill>
      </div>
      <div className="mt-2">
        <BarGauge
          label={`$${row.spent_usd.toFixed(2)} spent`}
          pct={row.cap_usd > 0 ? row.pct : 0}
          tone={tone === "muted" ? "neutral" : tone}
          caption={row.cap_usd > 0 ? `of $${row.cap_usd.toFixed(2)} weekly` : "uncapped"}
        />
      </div>
      {row.legacy && (
        <p className="mt-1 text-[10px]" style={{ color: "var(--ctl-muted)" }}>
          historical — pre-key-tracking
        </p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <span className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>$</span>
        <input
          value={cap}
          onChange={(e) => setCap(e.target.value)}
          placeholder="weekly cap"
          inputMode="decimal"
          className="tnum w-24 rounded-md border px-2 py-1 text-[12px] outline-none"
          style={{ background: "#131316", borderColor: "var(--ctl-border)", color: "var(--ctl-text)" }}
        />
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="rounded-md px-2 py-1 text-[11px] disabled:opacity-50"
          style={{ background: "#27272a", color: "var(--ctl-text)" }}
        >
          set
        </button>
      </div>
    </div>
  );
}

function CeilingRow({ c, onSaved }: { c: Ceiling; onSaved: () => void }) {
  const isStorage = c.key === "storage";
  const isMps = c.key === "mps";
  const ceilingVal = isStorage ? c.floor_gb : isMps ? c.ceiling_gb : c.ceiling_pct;
  const [val, setVal] = useState(ceilingVal ? String(ceilingVal) : "");
  const field =
    c.key === "memory" ? "mem_refuse_pct" : c.key === "cpu" ? "cpu_refuse_pct" : isStorage ? "min_free_disk_gb" : "max_mps_mem_gb";
  const save = useMutation({
    mutationFn: () => controlPost("resource-limits", { [field]: Number(val) || 0 }),
    onSuccess: onSaved,
  });
  const current = isStorage || isMps ? c.current_gb : c.current_pct;
  return (
    <div className="rounded-lg border p-3" style={{ background: "#0c0c0f", borderColor: "var(--ctl-border)" }}>
      <div className="flex items-center justify-between">
        <span className="text-[13px]">{c.label}</span>
        <span className="tnum text-[12px]" style={{ color: "var(--ctl-muted)" }}>
          {current === null || current === undefined ? (c.note ?? "n/a") : `${current} ${c.unit}`}
        </span>
      </div>
      <p className="mt-1 text-[11px]" style={{ color: "var(--ctl-muted)" }}>
        {c.action}
      </p>
      <div className="mt-2 flex items-center gap-2">
        <input
          value={val}
          onChange={(e) => setVal(e.target.value)}
          placeholder={isStorage ? "min free GB" : isMps ? "max GB" : "max %"}
          inputMode="decimal"
          className="tnum w-24 rounded-md border px-2 py-1 text-[12px] outline-none"
          style={{ background: "#131316", borderColor: "var(--ctl-border)", color: "var(--ctl-text)" }}
        />
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="rounded-md px-2 py-1 text-[11px] disabled:opacity-50"
          style={{ background: "#27272a", color: "var(--ctl-text)" }}
        >
          set
        </button>
        <span className="text-[10px]" style={{ color: "var(--ctl-muted)" }}>0 = off</span>
      </div>
    </div>
  );
}
