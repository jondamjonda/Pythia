"use client";

import { useEffect, useRef } from "react";
import type { ControlEvent } from "@/lib/control";
import { MODULE_COLOR, normLevel } from "@/lib/control";

const LEVEL_COLOR = {
  info: "var(--ctl-muted)",
  warn: "var(--ctl-warn)",
  error: "var(--ctl-critical)",
} as const;

function hhmmss(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString("en-GB", { hour12: false });
  } catch {
    return "--:--:--";
  }
}

// A live, auto-scrolling event log (UI Build Plan §6). On a module panel pass already-filtered events
// (the panel's prefix whitelist); on the overview pass everything and show the colored module column.
export function LiveLogStream({
  events,
  connected,
  height = 340,
  showModule = true,
  emptyHint = "waiting for events…",
}: {
  events: ControlEvent[];
  connected: boolean;
  height?: number;
  showModule?: boolean;
  emptyHint?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  return (
    <div className="flex flex-col" style={{ height }}>
      <div
        ref={ref}
        className="tnum flex-1 overflow-y-auto rounded-md border text-[12px]"
        style={{ background: "#0c0c0f", borderColor: "var(--ctl-border)" }}
      >
        {events.length === 0 ? (
          <div className="p-3 text-[12px]" style={{ color: "var(--ctl-muted)" }}>
            {connected ? emptyHint : "connecting…"}
          </div>
        ) : (
          events.map((e, i) => {
            const lvl = normLevel(e.level);
            return (
              <div
                key={`${e.timestamp}-${i}`}
                className="flex items-start gap-2 border-b px-2 py-1 leading-tight"
                style={{ borderColor: "#1c1c20" }}
                title={e.diagnostic_message}
              >
                <span style={{ color: "var(--ctl-muted)" }}>{hhmmss(e.timestamp)}</span>
                {showModule && (
                  <span
                    className="w-16 shrink-0 truncate text-[10px] uppercase"
                    style={{ color: e.module ? MODULE_COLOR[e.module] : "var(--ctl-muted)" }}
                  >
                    {e.module ?? "—"}
                  </span>
                )}
                <span
                  className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ background: LEVEL_COLOR[lvl] }}
                />
                <span className="flex-1" style={{ color: lvl === "info" ? "var(--ctl-text)" : LEVEL_COLOR[lvl] }}>
                  {e.friendly_message}
                </span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
