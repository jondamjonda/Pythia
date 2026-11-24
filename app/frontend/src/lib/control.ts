// Control-room client helpers (UI Build Plan §2.2). Everything goes through the SAME-ORIGIN Next proxy
// (/control/api/*), never directly to the FastAPI backend — so the control token stays server-side.
// Pure helpers only (no React) so this is importable anywhere; the live hooks live in client components.

export const CONTROL_BASE = "/control/api";

export async function controlGet<T>(path: string): Promise<T> {
  const r = await fetch(`${CONTROL_BASE}/${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`);
  return (await r.json()) as T;
}

export async function controlPost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${CONTROL_BASE}/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}`);
  return (await r.json()) as T;
}

// ── shapes (slim mirrors of the backend, only the fields the UI reads) ──────────────────────────────
export type Pressure = "normal" | "elevated" | "high" | "unknown";
export type Level = "info" | "warn" | "warning" | "error";
export type ModuleName = "nexus" | "pythia" | "polarius" | "system";

export interface Mps {
  available: boolean;
  stale?: boolean;
  age_s?: number;
  allocated_bytes?: number | null;
  reason?: string | null;
}

export interface SystemSample {
  ts: string;
  cpu: { percent?: number; count?: number; load_1m?: number; per_core?: number[] };
  memory: { total_bytes: number; used_bytes: number; available_bytes: number; percent: number } | null;
  swap: { total_bytes: number; used_bytes: number; percent: number } | null;
  disk: { mount: string; total_bytes: number; used_bytes: number; free_bytes: number; percent: number } | null;
  process: Record<string, number> | null;
  mps: Mps;
  pressure: Pressure;
}

export interface ProcGroup {
  key: string;
  label: string;
  uss_bytes: number;
  rss_bytes: number;
  n_procs: number;
  percent_of_ram: number;
  uss_exact: boolean;
}

export interface SystemSnapshot {
  resources: SystemSample | null;
  collector: {
    running: boolean;
    ticks: number;
    collector_errors: number;
    ring_points: number;
    persist_enabled: boolean;
    as_of_age_s: number | null;
  };
  memory_segmentation: { groups?: ProcGroup[]; as_of?: string } | null;
  mps: Mps | null;
  pressure: Pressure | null;
  storage: { data_dir_bytes: number; limit_bytes: number; over_limit: boolean; percent: number };
  kill_switch: { active?: boolean; reason?: string | null; engaged_at?: string | null };
}

export interface HistoryPoint {
  ts: string;
  cpu_pct: number | null;
  mem_pct: number | null;
  swap_pct: number | null;
  disk_pct: number | null;
}
export interface HistoryResponse {
  source: string;
  window_seconds?: number;
  points: HistoryPoint[];
  error?: string;
}

export interface ControlEvent {
  timestamp: string;
  project_id: string | null;
  module: ModuleName | null;
  level: Level;
  code: string;
  friendly_message: string;
  diagnostic_message: string;
  payload: Record<string, unknown>;
}

// ── formatters ──────────────────────────────────────────────────────────────────────────────────────
export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

export function fmtPct(n: number | null | undefined, digits = 0): string {
  return n === null || n === undefined ? "—" : `${n.toFixed(digits)}%`;
}

export function fmtAge(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

/** Normalize the backend's level ("warn" vs structlog's "warning") to the 3 semantic states. */
export function normLevel(l: Level | string): "info" | "warn" | "error" {
  if (l === "error") return "error";
  if (l === "warn" || l === "warning") return "warn";
  return "info";
}

export const MODULE_COLOR: Record<string, string> = {
  nexus: "#5eb0ef",
  pythia: "#a78bfa",
  polarius: "#34d399",
  system: "#fbbf24",
};

export function pressureTone(p: Pressure | null | undefined): "ok" | "warn" | "critical" | "muted" {
  if (p === "high") return "critical";
  if (p === "elevated") return "warn";
  if (p === "normal") return "ok";
  return "muted";
}
