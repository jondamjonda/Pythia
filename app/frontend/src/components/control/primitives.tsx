"use client";

import type { ReactNode } from "react";

// The shared control-room primitive kit (UI Build Plan §6). Calm, dense, tabular numbers; semantic color
// only for ok/warn/critical. Every metric carries an `as-of` age so a frozen collector is visibly
// distinct from a real value (golden rule #6 — no fake zeros).

export type Tone = "ok" | "warn" | "critical" | "muted" | "neutral";

const TONE_VAR: Record<Tone, string> = {
  ok: "var(--ctl-ok)",
  warn: "var(--ctl-warn)",
  critical: "var(--ctl-critical)",
  muted: "var(--ctl-muted)",
  neutral: "var(--ctl-text)",
};

export function toneColor(t: Tone): string {
  return TONE_VAR[t];
}

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border p-4 ${className}`}
      style={{ background: "var(--ctl-panel)", borderColor: "var(--ctl-border)" }}
    >
      {(title || right) && (
        <header className="mb-3 flex items-center justify-between">
          <h2 className="text-[13px] font-medium tracking-wide" style={{ color: "var(--ctl-muted)" }}>
            {title}
          </h2>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function AsOf({ ageS, stale }: { ageS?: number | null; stale?: boolean }) {
  if (ageS === null || ageS === undefined) {
    return (
      <span className="tnum text-[10px]" style={{ color: "var(--ctl-muted)" }}>
        no data
      </span>
    );
  }
  const isStale = stale ?? ageS > 15;
  return (
    <span
      className="tnum text-[10px]"
      style={{ color: isStale ? "var(--ctl-warn)" : "var(--ctl-muted)" }}
      title={`last sample ${ageS.toFixed(1)}s ago`}
    >
      {isStale ? `stale ${ageS.toFixed(0)}s` : "live"}
    </span>
  );
}

export function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  const c = toneColor(tone);
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium"
      style={{ color: c, background: `color-mix(in srgb, ${c} 14%, transparent)` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: c }} />
      {children}
    </span>
  );
}

export function MetricTile({
  label,
  value,
  unit,
  sub,
  tone = "neutral",
  ageS,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
  tone?: Tone;
  ageS?: number | null;
}) {
  return (
    <div
      className="rounded-lg border p-3"
      style={{ background: "var(--ctl-panel)", borderColor: "var(--ctl-border)" }}
    >
      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wide" style={{ color: "var(--ctl-muted)" }}>
          {label}
        </span>
        {ageS !== undefined && <AsOf ageS={ageS} />}
      </div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="tnum text-2xl font-semibold" style={{ color: toneColor(tone) }}>
          {value}
        </span>
        {unit && (
          <span className="tnum text-sm" style={{ color: "var(--ctl-muted)" }}>
            {unit}
          </span>
        )}
      </div>
      {sub && (
        <div className="tnum mt-0.5 text-[11px]" style={{ color: "var(--ctl-muted)" }}>
          {sub}
        </div>
      )}
    </div>
  );
}

export function BarGauge({
  label,
  pct,
  tone = "neutral",
  caption,
}: {
  label: string;
  pct: number | null | undefined;
  tone?: Tone;
  caption?: ReactNode;
}) {
  const c = toneColor(tone);
  const v = pct === null || pct === undefined ? null : Math.max(0, Math.min(100, pct));
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[12px]">
        <span style={{ color: "var(--ctl-muted)" }}>{label}</span>
        <span className="tnum" style={{ color: v === null ? "var(--ctl-muted)" : c }}>
          {v === null ? "—" : `${v.toFixed(0)}%`}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full" style={{ background: "#27272a" }}>
        <div
          className="h-full rounded-full transition-[width] duration-500"
          style={{ width: `${v ?? 0}%`, background: c }}
        />
      </div>
      {caption && (
        <div className="tnum mt-1 text-[10px]" style={{ color: "var(--ctl-muted)" }}>
          {caption}
        </div>
      )}
    </div>
  );
}

export function Sparkline({
  data,
  width = 240,
  height = 36,
  tone = "neutral",
}: {
  data: (number | null)[];
  width?: number;
  height?: number;
  tone?: Tone;
}) {
  const pts = data.filter((d): d is number => d !== null);
  if (pts.length < 2) {
    return (
      <div className="text-[10px]" style={{ color: "var(--ctl-muted)", height }}>
        gathering…
      </div>
    );
  }
  const max = 100; // these are percentages
  const min = 0;
  const span = max - min || 1;
  const step = width / (data.length - 1);
  const path = data
    .map((d, i) => {
      const y = d === null ? height : height - ((d - min) / span) * height;
      return `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} className="block">
      <path d={path} fill="none" stroke={toneColor(tone)} strokeWidth={1.5} />
    </svg>
  );
}
