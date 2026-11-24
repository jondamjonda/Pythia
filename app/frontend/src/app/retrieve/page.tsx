"use client";

// Free-text retrieval page — exposes /internal/retrieve so the lawyer can
// poke the corpus directly. Surfaces the calibrated refusal banner when
// rerank_score < threshold (plan §12.7 / Day 13).

import { useState } from "react";

import { CitationLink } from "@/components/CitationLink";
import { api, type RetrieveResponse } from "@/lib/api";

export default function RetrievePage() {
  // This debug surface searches the app (regsuper) corpus → no `?corpus=` (undefined = app DB), so the
  // CitationLink deep-link is byte-identical to before. Threaded through so a future pythia-corpus debug
  // search preserves the corpus its citations came from.
  const resultCorpus: string | undefined = undefined;
  const [query, setQuery] = useState("How many days notice for an EU AIFM marketing to the CNB?");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RetrieveResponse | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.retrieve({
        query,
        jurisdiction: ["EU", "CZ"],
        top_n_rerank: 10,
        expand_top_n: 0,
      });
      setResult(r);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Free-text retrieval</h1>
      <p className="mb-6 text-sm text-slate-500">
        Search the corpus with a natural-language query. The system applies a calibrated refusal
        threshold — out-of-scope queries return &quot;insufficient confidence&quot; rather than a wrong answer.
      </p>

      <form onSubmit={onSubmit} className="mb-6 space-y-3">
        <textarea
          className="h-24 w-full rounded border px-3 py-2"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask anything…"
        />
        <button
          type="submit"
          disabled={loading || !query.trim()}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {loading ? "Searching…" : "Search →"}
        </button>
      </form>

      {error && (
        <div className="mb-4 rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </div>
      )}

      {result?.refused && (
        <div className="mb-4 rounded border-2 border-amber-400 bg-amber-50 p-4">
          <h2 className="mb-1 font-semibold text-amber-900">⚠ Refused — insufficient confidence</h2>
          <p className="text-sm text-amber-800">{result.reason}</p>
          <p className="mt-2 text-xs text-amber-700">
            Top rerank score:{" "}
            <code>{result.top_rerank_score?.toFixed(3) ?? "n/a"}</code>. The system will not return
            an answer at this confidence level. Refine the query, scope to a specific regulation,
            or accept that the corpus may not cover this topic.
          </p>
        </div>
      )}

      {result && result.flagged && !result.refused && (
        <div className="mb-4 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Low confidence — results shown but the rerank score is below the calibrated threshold
          (<code>{result.top_rerank_score?.toFixed(3) ?? "n/a"}</code>). Treat as a lead, not an answer.
        </div>
      )}

      {result && !result.refused && result.results.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-medium text-slate-600">
            Top {result.results.length} results — latency {result.diagnostics.latency_ms}ms
          </h2>
          <ol className="space-y-2">
            {result.results.map((r) => (
              <li key={r.provision_id} className="rounded border bg-white p-3 shadow-sm">
                <div className="flex items-center justify-between">
                  <div>
                    <CitationLink provisionId={r.provision_id} label={`${r.external_id ?? "?"} ${r.label ?? ""}`} corpus={resultCorpus} />
                    <p className="mt-1 text-xs text-slate-500">{r.title}</p>
                  </div>
                  <span className="text-xs text-slate-400">
                    rerank {r.rerank_score?.toFixed(3) ?? "—"}
                  </span>
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}
    </main>
  );
}
