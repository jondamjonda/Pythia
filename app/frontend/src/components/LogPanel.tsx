// LogPanel — operator log drawer (plan §16.4). Subscribes to the FastAPI
// SSE stream at /api/projects/{id}/events. Two tabs: Activity (friendly
// templates) and Diagnostics (raw structured rows + Copy button). Auto-opens
// on the first error event.
"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

interface Event {
  timestamp: string;
  level: "info" | "warn" | "error";
  code: string;
  friendly_message: string;
  diagnostic_message: string;
  payload: Record<string, unknown>;
}

const LEVEL_STYLE: Record<string, string> = {
  info: "text-slate-700",
  warn: "text-amber-700",
  error: "text-rose-700 font-medium",
};

// Hydrate the persisted preference once, in a lazy initializer, so we never call setState inside an
// effect (react-hooks/set-state-in-effect). Guarded for SSR (no `window`): falls back to the default,
// which matches the server-rendered markup before the client takes over.
function persistedOpen(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem("logpanel.open") === "1";
}
function persistedTab(): "activity" | "diagnostics" {
  if (typeof window === "undefined") return "activity";
  const t = localStorage.getItem("logpanel.tab");
  return t === "diagnostics" || t === "activity" ? t : "activity";
}

export function LogPanel({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(persistedOpen);
  const [tab, setTab] = useState<"activity" | "diagnostics">(persistedTab);
  const [events, setEvents] = useState<Event[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Persist open/tab in localStorage so the lawyer's preference sticks.
  useEffect(() => {
    localStorage.setItem("logpanel.open", open ? "1" : "0");
  }, [open]);
  useEffect(() => {
    localStorage.setItem("logpanel.tab", tab);
  }, [tab]);

  useEffect(() => {
    const es = new EventSource(api.eventStreamUrl(projectId));
    es.onmessage = (m) => {
      try {
        const evt = JSON.parse(m.data) as Event;
        setEvents((prev) => [...prev.slice(-499), evt]);
        if (evt.level === "error") setOpen(true);
      } catch {
        // Ignore malformed frames.
      }
    };
    es.onerror = () => {
      // Browser auto-reconnects; nothing to do here.
    };
    return () => es.close();
  }, [projectId]);

  // Auto-scroll to newest when at the bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [events]);

  const copyJSON = async () => {
    await navigator.clipboard.writeText(JSON.stringify(events, null, 2));
  };
  const copyText = async () => {
    const txt = events
      .map((e) => `${e.timestamp}  ${e.level.toUpperCase()}  ${e.code}  ${e.diagnostic_message}`)
      .join("\n");
    await navigator.clipboard.writeText(txt);
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 rounded-full bg-slate-900 px-4 py-2 text-sm text-white shadow-lg hover:bg-slate-800"
      >
        Activity ({events.length})
      </button>
    );
  }

  return (
    <aside className="fixed bottom-0 right-0 top-0 z-40 flex w-full max-w-md flex-col border-l bg-white shadow-2xl">
      <header className="flex items-center justify-between border-b px-4 py-2">
        <h2 className="text-sm font-semibold">Activity log</h2>
        <button onClick={() => setOpen(false)} className="text-slate-400 hover:text-slate-700">
          ✕
        </button>
      </header>
      <div className="border-b px-4 py-1">
        <button
          onClick={() => setTab("activity")}
          className={`mr-3 text-xs ${tab === "activity" ? "font-semibold underline" : "text-slate-500"}`}
        >
          Activity
        </button>
        <button
          onClick={() => setTab("diagnostics")}
          className={`mr-3 text-xs ${tab === "diagnostics" ? "font-semibold underline" : "text-slate-500"}`}
        >
          Diagnostics
        </button>
        {tab === "diagnostics" && (
          <span className="float-right space-x-2 text-xs">
            <button onClick={copyJSON} className="rounded border px-1.5 py-0.5 hover:bg-slate-50">
              Copy JSON
            </button>
            <button onClick={copyText} className="rounded border px-1.5 py-0.5 hover:bg-slate-50">
              Copy text
            </button>
          </span>
        )}
      </div>
      <div ref={scrollRef} className="flex-1 overflow-auto px-3 py-2 text-xs">
        {events.length === 0 && (
          <p className="py-8 text-center text-slate-400">Waiting for events…</p>
        )}
        {events.map((e, i) => (
          <div key={i} className={`mb-1 ${LEVEL_STYLE[e.level] ?? ""}`}>
            {tab === "activity" ? (
              <div>
                <span className="text-slate-400">{e.timestamp.slice(11, 19)}</span>{" "}
                {e.friendly_message || e.code}
              </div>
            ) : (
              <div className="font-mono">
                <span className="text-slate-400">{e.timestamp.slice(11, 23)}</span>{" "}
                <span>{e.code}</span>{" "}
                <span className="text-slate-500">{e.diagnostic_message}</span>
              </div>
            )}
          </div>
        ))}
      </div>
    </aside>
  );
}
