"use client";

// Source viewer — plan §13.5 Day 14. Renders one provision with its
// hierarchy breadcrumb walking up the parent_id chain, plus any cross-
// references the corpus knows about.

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, type ProvisionView } from "@/lib/api";

export default function SourcePage({
  params,
  searchParams,
}: {
  params: Promise<{ provision_id: string }>;
  searchParams: Promise<{ corpus?: string }>;
}) {
  const [provisionId, setProvisionId] = useState<string | null>(null);
  // Which corpus to read (carried in ?corpus=). Stays with the page so breadcrumb /
  // cross-reference navigation keeps reading the same corpus as the act-graph diagram.
  const [corpus, setCorpus] = useState<string | undefined>(undefined);
  useEffect(() => {
    params.then((p) => setProvisionId(p.provision_id));
  }, [params]);
  useEffect(() => {
    searchParams.then((s) => setCorpus(s.corpus));
  }, [searchParams]);

  const [view, setView] = useState<ProvisionView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!provisionId) return;
    // Yield once so the first setState is not synchronous inside the effect body
    // (react-hooks/set-state-in-effect). Behaviour-equivalent: error clears, then the fetch resolves.
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setError(null);
      try {
        const v = await api.getProvision(provisionId, corpus);
        if (!cancelled) setView(v);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [provisionId, corpus]);

  // Preserve ?corpus= across in-page navigation (breadcrumb + cross-references).
  const corpusQuery = corpus ? `?corpus=${encodeURIComponent(corpus)}` : "";

  if (error) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-8 text-rose-700">
        Could not load provision: {error}
      </main>
    );
  }
  if (!view) return <main className="p-8 text-slate-500">Loading source…</main>;

  // Build the top-down breadcrumb (ancestors are child→root, reverse for display).
  const trail = [...view.ancestors].reverse();

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <nav className="mb-4 text-sm text-slate-500">
        <button onClick={() => history.back()} className="hover:underline">
          ← Back
        </button>
      </nav>

      <header className="mb-4 border-b pb-4">
        <p className="text-xs uppercase tracking-wide text-slate-400">
          {view.source_publisher} · {view.legal_hierarchy} · {view.jurisdiction}
        </p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">{view.source_title}</h1>
        <p className="mt-1 font-mono text-xs text-slate-500">{view.source_external_id ?? ""}</p>
        <TrustBadges
          bindingnessTier={view.bindingness_tier}
          temporalStatus={view.temporal_status}
        />
      </header>

      {trail.length > 0 && (
        <nav className="mb-4 text-sm text-slate-500">
          {trail.map((a, i) => (
            <span key={a.id}>
              <Link href={`/sources/${a.id}${corpusQuery}`} className="hover:underline">
                {a.label ?? "(unlabelled)"}
              </Link>
              {i < trail.length - 1 ? " › " : " › "}
            </span>
          ))}
          <span className="font-medium text-slate-800">{view.label ?? "(unlabelled)"}</span>
        </nav>
      )}

      <article className="mb-6 rounded-lg border bg-white p-5 shadow-sm">
        <h2 className="mb-3 text-lg font-semibold">{view.label ?? "(unlabelled provision)"}</h2>
        {view.text_format === "gfm" ? (
          <ProvisionMarkdown text={view.text} />
        ) : (
          <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed">{view.text}</pre>
        )}
      </article>

      {view.cross_references.length > 0 && (
        <section className="mb-6">
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-600">
            Cross-references
          </h3>
          <ul className="space-y-1 text-sm">
            {view.cross_references.map((x) => (
              <li key={x.id}>
                <Link href={`/sources/${x.id}${corpusQuery}`} className="text-sky-700 hover:underline">
                  {x.label ?? x.hierarchy_path}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="mt-10 text-xs text-slate-400">
        Provision id: <code>{view.id}</code>
      </footer>
    </main>
  );
}

// B2b-13/12 — provision trust/temporal badges. NULL-WHEN-NULL: until the corpus backfill populates
// these (0/7895 today) BOTH props are null and this renders NOTHING — we never fabricate a "hard-law"
// or "in-force" badge the data doesn't support (§4.2 trust-vocab-mismatch failure mode).
const BINDINGNESS_LABELS: Record<string, { label: string; cls: string }> = {
  "hard-law": { label: "Hard law", cls: "bg-emerald-50 text-emerald-800 ring-emerald-200" },
  oop: { label: "Official position", cls: "bg-sky-50 text-sky-800 ring-sky-200" },
  "soft-law": { label: "Soft law", cls: "bg-amber-50 text-amber-800 ring-amber-200" },
  interpretive: { label: "Interpretive", cls: "bg-slate-100 text-slate-700 ring-slate-200" },
};
const TEMPORAL_LABELS: Record<string, { label: string; cls: string }> = {
  "in-force": { label: "In force", cls: "bg-emerald-50 text-emerald-800 ring-emerald-200" },
  "future-effective": { label: "Future-effective", cls: "bg-sky-50 text-sky-800 ring-sky-200" },
  annulled: { label: "Annulled", cls: "bg-rose-50 text-rose-800 ring-rose-200" },
  superseded: { label: "Superseded", cls: "bg-slate-100 text-slate-600 ring-slate-200" },
};

function TrustBadges({
  bindingnessTier,
  temporalStatus,
}: {
  bindingnessTier: string | null;
  temporalStatus: string | null;
}) {
  const b = bindingnessTier ? BINDINGNESS_LABELS[bindingnessTier] : undefined;
  const t = temporalStatus ? TEMPORAL_LABELS[temporalStatus] : undefined;
  // Render only what the data actually supports (an unrecognised vocab value shows the raw value, never
  // a guessed label). Nothing to show → no element at all.
  if (!bindingnessTier && !temporalStatus) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {bindingnessTier && (
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${b?.cls ?? "bg-slate-100 text-slate-700 ring-slate-200"}`}
        >
          {b?.label ?? bindingnessTier}
        </span>
      )}
      {temporalStatus && (
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${t?.cls ?? "bg-slate-100 text-slate-700 ring-slate-200"}`}
        >
          {t?.label ?? temporalStatus}
        </span>
      )}
    </div>
  );
}

// Very lightweight markdown renderer — we only need headings, paragraphs,
// list items, and bold/italic, which is what `regsuper.ingest.sentences`
// produces. Anything more elaborate stays as raw text so legal review can
// see what the LLM saw.
function ProvisionMarkdown({ text }: { text: string }) {
  // Split paragraphs by double newline, headings by leading "#".
  const blocks = text.split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  return (
    <div className="prose prose-slate max-w-none text-sm leading-relaxed">
      {blocks.map((b, i) => {
        if (b.startsWith("### ")) return <h4 key={i}>{b.slice(4)}</h4>;
        if (b.startsWith("## ")) return <h3 key={i}>{b.slice(3)}</h3>;
        if (b.startsWith("# ")) return <h2 key={i}>{b.slice(2)}</h2>;
        if (b.startsWith("- ")) {
          const items = b.split(/\n- /).map((s, idx) => (idx === 0 ? s.slice(2) : s));
          return (
            <ul key={i}>
              {items.map((it, k) => (
                <li key={k}>{it}</li>
              ))}
            </ul>
          );
        }
        return <p key={i}>{b}</p>;
      })}
    </div>
  );
}
