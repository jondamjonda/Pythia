"use client";

// AQ-05 — municipality autocomplete for the profile form.
//
// The SME types the town name; this debounces the query, calls the read-only
// GET /api/municipalities?q= search, and shows the matches. On select it stores
// the REAL validated `code` (never the free text) via `onSelect(code)`, and
// shows the picked name back to the user. The parent binds the stored `code`
// into ProfilePayload.municipality_code — the server still 422s an unknown code
// on save, so a code stored here is always a real gazetteer entry.
//
// DEGRADES GRACEFULLY: an empty query shows nothing (no request); a failed fetch
// clears the list and never throws (the field stays usable, just without
// suggestions). Clearing the input clears the stored code (back to unset).

import { useEffect, useId, useRef, useState } from "react";

import { api, type MunicipalityMatch } from "@/lib/api";

const DEBOUNCE_MS = 200;
// Below this the query is a no-op (too short to be a useful prefix).
const MIN_QUERY = 2;

export interface MunicipalityAutocompleteProps {
  // The currently stored code (or null/"" when unset) — the source of truth.
  value: string | null;
  // Called with the picked municipality's code, or "" when the field is cleared.
  onSelect: (code: string) => void;
  // Optional initial display text (e.g. a name resolved from a stored code).
  initialLabel?: string;
  label?: string;
  placeholder?: string;
}

export function MunicipalityAutocomplete({
  value,
  onSelect,
  initialLabel = "",
  label = "Obec",
  placeholder = "Začněte psát název obce…",
}: MunicipalityAutocompleteProps) {
  const [text, setText] = useState(initialLabel);
  const [matches, setMatches] = useState<MunicipalityMatch[]>([]);
  const [open, setOpen] = useState(false);
  // Guards against a stale debounced response overwriting a newer one.
  const reqSeq = useRef(0);
  // Stable unique id linking the combobox → its listbox (a11y; unique per instance).
  const LISTBOX_ID = useId();

  useEffect(() => {
    const q = text.trim();
    const seq = ++reqSeq.current;
    // All state writes happen INSIDE the debounced callback (never synchronously
    // in the effect body) — a too-short prefix simply clears the suggestions
    // rather than issuing a request.
    const t = setTimeout(async () => {
      if (q.length < MIN_QUERY) {
        if (seq === reqSeq.current) setMatches([]);
        return;
      }
      try {
        const rows = await api.searchMunicipalities(q);
        // Ignore if a newer keystroke has superseded this request.
        if (seq !== reqSeq.current) return;
        setMatches(rows);
        setOpen(true);
      } catch {
        // Degrade gracefully: no suggestions, field stays usable, never throws.
        if (seq !== reqSeq.current) return;
        setMatches([]);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [text]);

  function pick(m: MunicipalityMatch) {
    setText(m.name);
    setMatches([]);
    setOpen(false);
    onSelect(m.code); // store the REAL code, never the free text
  }

  function onChange(next: string) {
    setText(next);
    setOpen(true);
    // Editing invalidates any previously-picked code — the parent must not keep
    // a stale code that no longer matches the typed text.
    if (value) onSelect("");
  }

  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{label}</span>
      <div className="relative">
        <input
          type="text"
          role="combobox"
          aria-controls={LISTBOX_ID}
          aria-expanded={open && matches.length > 0}
          aria-autocomplete="list"
          className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900 outline-none focus:border-slate-400"
          value={text}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => setOpen(true)}
        />
        {open && matches.length > 0 && (
          <ul
            id={LISTBOX_ID}
            role="listbox"
            className="absolute z-10 mt-1 max-h-64 w-full overflow-auto rounded border border-slate-200 bg-white shadow-lg"
          >
            {matches.map((m) => (
              <li key={m.code} role="option" aria-selected={m.code === value}>
                <button
                  type="button"
                  onClick={() => pick(m)}
                  className="flex w-full items-baseline justify-between gap-2 px-3 py-2 text-left text-sm text-slate-900 hover:bg-slate-100"
                >
                  <span>{m.name}</span>
                  {(m.okres || m.kraj) && (
                    <span className="text-xs text-slate-400">
                      {[m.okres, m.kraj].filter(Boolean).join(" · ")}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </label>
  );
}
