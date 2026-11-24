"use client";

import { useEffect, useRef, useState } from "react";
import type { ControlEvent } from "@/lib/control";

// The control room's ONE EventSource (UI Build Plan §0.2). Connects to the same-origin SSE proxy, keeps
// a bounded ring of recent control events, reconnects on error. An optional `prefixes` whitelist filters
// to a single module panel (MECE) without opening a second stream.
export function useSystemStream(opts?: { max?: number; prefixes?: string[] }) {
  const max = opts?.max ?? 300;
  const prefixesRef = useRef<string[] | undefined>(opts?.prefixes);
  // React-19: writing a ref during render is disallowed (react-hooks/refs). Keep the latest
  // prefixes in the ref via an effect so the (deps-less) EventSource onmessage reads the current
  // whitelist without re-opening the stream when `prefixes` changes identity.
  useEffect(() => {
    prefixesRef.current = opts?.prefixes;
  });

  const [events, setEvents] = useState<ControlEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let stopped = false;
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (stopped) return;
      es = new EventSource("/control/api/events");
      es.onopen = () => setConnected(true);
      es.onmessage = (e) => {
        if (!e.data) return; // keepalive ping
        try {
          const ev = JSON.parse(e.data) as ControlEvent;
          const pf = prefixesRef.current;
          if (pf && !pf.some((p) => ev.code.startsWith(p))) return;
          setEvents((prev) => {
            const next = [...prev, ev];
            return next.length > max ? next.slice(next.length - max) : next;
          });
        } catch {
          /* malformed line — ignore */
        }
      };
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (!stopped) retry = setTimeout(connect, 3_000);
      };
    };

    connect();
    return () => {
      stopped = true;
      es?.close();
      if (retry) clearTimeout(retry);
    };
  }, [max]);

  return { events, connected };
}
