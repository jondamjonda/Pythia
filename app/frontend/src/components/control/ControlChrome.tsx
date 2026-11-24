"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { controlGet, fmtAge, fmtPct, pressureTone, type SystemSnapshot } from "@/lib/control";
import { StatusPill, toneColor } from "./primitives";

// Control-room chrome (UI Build Plan §3.2): a persistent left rail whose items carry live micro-status,
// and a fixed bottom status bar surfacing the worst signal everywhere. Phase 0 lights up Overview +
// System; the module + API panels arrive in later phases (shown disabled until then).
const NAV: { href: string; label: string; group?: string; enabled: boolean; soon?: boolean }[] = [
  { href: "/control", label: "Overview", enabled: true },
  { href: "/control/nexus", label: "Nexus", group: "modules", enabled: true },
  { href: "/control/graph", label: "Graph", group: "modules", enabled: true },
  { href: "/control/ingestion", label: "Auto-ingestion", group: "modules", enabled: true },
  { href: "/control/pythia", label: "Pythia", enabled: true },
  { href: "/control/polarius", label: "Polarius", enabled: true },
  { href: "/control/system", label: "System", group: "platform", enabled: true },
  { href: "/control/api", label: "API & caps", enabled: true },
];

export function ControlChrome({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { data } = useQuery({
    queryKey: ["system"],
    queryFn: () => controlGet<SystemSnapshot>("system"),
    refetchInterval: 5_000,
  });

  const res = data?.resources ?? null;
  const pressure = data?.pressure ?? "unknown";
  const tone = pressureTone(pressure);
  const ks = (data?.kill_switch ?? {}) as { active?: boolean };
  const killed = Boolean(ks.active);
  const age = data?.collector?.as_of_age_s ?? null;

  return (
    <div className="control-shell flex min-h-screen flex-col">
      <div className="flex flex-1">
        {/* left rail */}
        <aside
          className="flex w-[220px] shrink-0 flex-col border-r"
          style={{ borderColor: "var(--ctl-border)", background: "var(--ctl-panel)" }}
        >
          <div className="flex items-center justify-between px-4 py-4">
            <span className="text-[13px] font-semibold tracking-wide">POLARIUS</span>
            <StatusPill tone={killed ? "critical" : tone}>
              {killed ? "halted" : pressure === "unknown" ? "…" : pressure}
            </StatusPill>
          </div>
          <nav className="flex flex-col gap-0.5 px-2 text-[13px]">
            {NAV.map((n) => {
              const active = pathname === n.href;
              const base = "rounded-md px-3 py-1.5 transition-colors";
              if (!n.enabled) {
                return (
                  <span
                    key={n.href}
                    className={`${base} cursor-default`}
                    style={{ color: "#3f3f46" }}
                  >
                    {n.label} <span className="text-[10px]">soon</span>
                  </span>
                );
              }
              return (
                <Link
                  key={n.href}
                  href={n.href}
                  className={base}
                  style={{
                    color: active ? "var(--ctl-text)" : "var(--ctl-muted)",
                    background: active ? "#1f1f23" : "transparent",
                  }}
                >
                  {n.label}
                </Link>
              );
            })}
          </nav>
          <div className="mt-auto px-4 py-3 text-[11px]" style={{ color: "var(--ctl-muted)" }}>
            <Link href="/" style={{ color: "var(--ctl-muted)" }}>
              ↗ Product
            </Link>
          </div>
        </aside>

        {/* main */}
        <main className="flex-1 overflow-y-auto p-6 pb-12">{children}</main>
      </div>

      {/* fixed status bar */}
      <footer
        className="tnum fixed inset-x-0 bottom-0 flex h-7 items-center gap-4 border-t px-4 text-[11px]"
        style={{ borderColor: "var(--ctl-border)", background: "var(--ctl-panel)", color: "var(--ctl-muted)" }}
      >
        <span style={{ color: toneColor(killed ? "critical" : tone) }}>
          ● {killed ? "KILL-SWITCH ENGAGED" : `health ${pressure}`}
        </span>
        <span>CPU {fmtPct(res?.cpu?.percent)}</span>
        <span>RAM {fmtPct(res?.memory?.percent)}</span>
        <span>swap {fmtPct(res?.swap?.percent)}</span>
        <span className="ml-auto">sample {fmtAge(age)} ago</span>
      </footer>
    </div>
  );
}
