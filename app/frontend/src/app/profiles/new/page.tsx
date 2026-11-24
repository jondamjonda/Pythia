"use client";

// Profile intake (LEDGER-M9) — "describe your business" → createProfile, then
// analyzeProfile (the SYNCHRONOUS cascade), then open the digest. Mirrors
// BusinessProfileIntake in src/regsuper/api/profiles.py.
//
// DEFAULT-OFF / BYTE-IDENTICAL: gated behind NEXT_PUBLIC_DIGEST_UI_ENABLED. Off
// → inert placeholder, nav never links here. The server gates profiles_enabled /
// digest_enabled return 404 when off → surfaced as the honest off-state.
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, type ProfilePayload } from "@/lib/api";
import { RECENT_PROFILES_KEY, type RecentProfile } from "@/app/profiles/page";
import { MunicipalityAutocomplete } from "@/components/municipality-autocomplete";

const DIGEST_UI_ENABLED =
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "true";

const LEGAL_FORMS: { value: string; label: string }[] = [
  { value: "sro", label: "s.r.o." },
  { value: "as", label: "a.s." },
  { value: "osvc", label: "OSVČ" },
  { value: "ks", label: "k.s." },
  { value: "vos", label: "v.o.s." },
  { value: "other", label: "jiné" },
];

const HEADCOUNT_BANDS = ["0", "1-9", "10-49", "50-249", "250+"];

const JURISDICTIONS: { value: string; label: string }[] = [
  { value: "CZ", label: "Česká republika" },
  { value: "EU", label: "EU" },
  { value: "CZ+EU", label: "ČR + EU" },
];

// The five compliance gates (BusinessProfile booleans) in plain language.
const GATES: { key: string; label: string }[] = [
  { key: "processes_personal_data", label: "Zpracovává osobní údaje" },
  { key: "handles_client_money", label: "Nakládá s penězi klientů" },
  { key: "has_real_estate", label: "Vlastní nebo spravuje nemovitosti" },
  { key: "cross_border", label: "Působí přeshraničně" },
  { key: "is_employer", label: "Je zaměstnavatel" },
];

function isNotFound(message: string): boolean {
  return / → 404:/.test(message) || /\b404\b/.test(message);
}

function rememberProfile(p: RecentProfile) {
  if (typeof window === "undefined") return;
  try {
    const raw = window.localStorage.getItem(RECENT_PROFILES_KEY);
    const arr: RecentProfile[] = raw ? JSON.parse(raw) : [];
    const next = [p, ...arr.filter((x) => x.id !== p.id)].slice(0, 20);
    window.localStorage.setItem(RECENT_PROFILES_KEY, JSON.stringify(next));
  } catch {
    /* localStorage best-effort — never block the flow */
  }
}

function OffState() {
  return (
    <main className="mx-auto max-w-2xl px-4 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
        Přehled pro firmy zatím není k dispozici
      </h1>
      <p className="mt-2 text-slate-500">Tato funkce je momentálně vypnutá.</p>
    </main>
  );
}

export default function NewProfilePage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [legalForm, setLegalForm] = useState("sro");
  const [ico, setIco] = useState("");
  const [naceCode, setNaceCode] = useState("");
  const [headcount, setHeadcount] = useState("1-9");
  const [jurisdiction, setJurisdiction] = useState("CZ");
  // AQ-05 — the validated obec code the autocomplete stores (never free text).
  const [municipalityCode, setMunicipalityCode] = useState("");
  const [gates, setGates] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<"idle" | "creating" | "analyzing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [serverOff, setServerOff] = useState(false);

  if (!DIGEST_UI_ENABLED) return <OffState />;
  if (serverOff) return <OffState />;

  function toggleGate(key: string) {
    setGates((g) => ({ ...g, [key]: !g[key] }));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setError(null);
    const payload: ProfilePayload = {
      name: name.trim(),
      legal_form: legalForm,
      ico: ico.trim() || undefined,
      nace_code: naceCode.trim() || undefined,
      headcount_band: headcount,
      primary_jurisdiction: jurisdiction,
      preferred_language: "cs",
      // Sent only when a real code was picked; the server 422s an unknown code.
      municipality_code: municipalityCode || undefined,
      characteristics: gates,
    };
    try {
      setBusy("creating");
      const profile = await api.createProfile(payload);
      rememberProfile({ id: profile.id, name: profile.name });
      // Run the synchronous cascade so the digest has findings to read.
      setBusy("analyzing");
      await api.analyzeProfile(profile.id, {});
      router.push(`/digest/${profile.id}`);
    } catch (err) {
      const msg = (err as Error).message;
      if (isNotFound(msg)) {
        setServerOff(true);
      } else {
        setError(msg);
        setBusy("idle");
      }
    }
  }

  const submitting = busy !== "idle";

  return (
    <main className="mx-auto max-w-2xl px-4 py-10">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight text-slate-900">
        Popište svou firmu
      </h1>
      <p className="mb-6 text-sm text-slate-500">
        Z těchto údajů sestavíme přehled povinností, které se na vaši firmu
        vztahují. Každá povinnost odkazuje na konkrétní předpis.
      </p>

      {error && (
        <div className="mb-4 rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </div>
      )}

      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Název firmy">
          <input
            className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900 outline-none focus:border-slate-400"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Právní forma">
            <select
              className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900"
              value={legalForm}
              onChange={(e) => setLegalForm(e.target.value)}
            >
              {LEGAL_FORMS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Počet zaměstnanců">
            <select
              className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900"
              value={headcount}
              onChange={(e) => setHeadcount(e.target.value)}
            >
              {HEADCOUNT_BANDS.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="IČO" hint="8 číslic (volitelné)">
            <input
              className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900 outline-none focus:border-slate-400"
              value={ico}
              onChange={(e) => setIco(e.target.value)}
              inputMode="numeric"
            />
          </Field>
          <Field label="NACE kód" hint="4 číslice (volitelné)">
            <input
              className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900 outline-none focus:border-slate-400"
              value={naceCode}
              onChange={(e) => setNaceCode(e.target.value)}
              inputMode="numeric"
            />
          </Field>
        </div>

        <Field label="Působnost">
          <select
            className="w-full rounded border border-slate-200 px-3 py-2 text-slate-900"
            value={jurisdiction}
            onChange={(e) => setJurisdiction(e.target.value)}
          >
            {JURISDICTIONS.map((j) => (
              <option key={j.value} value={j.value}>
                {j.label}
              </option>
            ))}
          </select>
        </Field>

        <MunicipalityAutocomplete
          value={municipalityCode || null}
          onSelect={setMunicipalityCode}
        />

        <fieldset>
          <legend className="mb-2 block text-sm font-medium text-slate-700">
            Co vaše firma dělá?
          </legend>
          <div className="space-y-2">
            {GATES.map((g) => (
              <label key={g.key} className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={!!gates[g.key]}
                  onChange={() => toggleGate(g.key)}
                  className="h-4 w-4 rounded border-slate-300"
                />
                {g.label}
              </label>
            ))}
          </div>
        </fieldset>

        <button
          type="submit"
          disabled={submitting || !name.trim()}
          className="rounded-lg bg-slate-900 px-5 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {busy === "creating"
            ? "Vytvářím profil…"
            : busy === "analyzing"
              ? "Analyzuji povinnosti…"
              : "Sestavit přehled →"}
        </button>
      </form>
    </main>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{label}</span>
      {hint && <span className="mb-1 block text-xs text-slate-400">{hint}</span>}
      {children}
    </label>
  );
}
