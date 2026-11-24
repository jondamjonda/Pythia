"use client";

// The public product console (UI Build Plan §6.1). Leads with the reliable, LLM-free retrieval over the
// app (regsuper) corpus, and shows a plain-language guide of WHAT'S LOADED so a user knows what they can
// ask. The honesty contract: an out-of-scope query is REFUSED, never answered with something wrong.
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type CorpusOverview, type RetrieveResponse, type AnswerResponse } from "@/lib/api";
import { AnswerView } from "@/components/answer/AnswerView";

// Phase-C: lead the console with the SYNTHESIZED ANSWER when the FE flag is on (so a user who asks a
// question gets the plain-language analysis, not just the ranked provisions). Flag off → the primary action
// stays "Find provisions" (byte-identical to before) and /answer is unreachable.
const ANSWER_PRIMARY =
  process.env.NEXT_PUBLIC_ANSWER_VIEW_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_ANSWER_VIEW_ENABLED === "true";

// Phase-C consumer answer surface (M8). Linked from the footer ONLY when the FE
// flag is on; default off → the footer is byte-identical to before, and /answer
// renders its own inert placeholder if reached directly.
const ANSWER_VIEW_ENABLED =
  process.env.NEXT_PUBLIC_ANSWER_VIEW_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_ANSWER_VIEW_ENABLED === "true";

// Phase-C SME digest surface (LEDGER-M9). Linked from the SAME footer block as
// the answer link, ONLY when the FE flag is on; default off → the footer is
// byte-identical to before, and /profiles renders its own inert placeholder.
const DIGEST_UI_ENABLED =
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "true";

const HIER_LABEL: Record<string, string> = {
  STATUTE: "Statute",
  CASE_LAW: "Case law",
  SOFT_LAW: "Soft law",
  REGIONAL_MUNICIPAL_PREDPIS: "Municipal",
};

// Plain-language areas, each tied to the acts that back it (so the size comes from live data) + an
// example question users can click to run. Acts not mapped here fall into "More" automatically.
const AREAS: { key: string; name: string; blurb: string; exts: string[]; example: string }[] = [
  { key: "funds", name: "Investment funds", blurb: "Running or marketing investment & alternative funds, including to retail investors.", exts: ["240/2013", "32011L0061"], example: "What must a fund depositary check before an AIF is marketed to retail investors in the Czech Republic?" },
  { key: "markets", name: "Capital markets & investment services", blurb: "Investment firms, trading venues, MiFID conduct, best execution and transparency.", exts: ["256/2004", "32014L0065", "32014R0600"], example: "What are the best-execution obligations for an investment firm under MiFID II?" },
  { key: "crypto", name: "Crypto-assets", blurb: "Issuing or providing services in crypto-assets under MiCA.", exts: ["32023R1114"], example: "What does MiCA require before offering a crypto-asset to the public?" },
  { key: "aml", name: "Anti-money-laundering", blurb: "Customer due diligence, reporting, and AML/CFT obligations.", exts: ["32015L0849", "253/2008"], example: "When is enhanced customer due diligence required?" },
  { key: "gdpr", name: "Data protection (GDPR)", blurb: "Processing personal data — lawful basis and data-subject rights.", exts: ["32016R0679"], example: "What lawful basis is needed to process client data for marketing?" },
  { key: "corporate", name: "Companies & corporate", blurb: "Company formation, governance, shares and directors' duties.", exts: ["90/2012"], example: "What are a director's core duties under the Business Corporations Act?" },
  { key: "civil", name: "Civil law & contracts", blurb: "Contracts, obligations and liability under the Civil Code.", exts: ["89/2012"], example: "What can make a contract clause invalid under the Civil Code?" },
];

export default function ProductConsole() {
  const [query, setQuery] = useState("");
  const [jur, setJur] = useState<string[]>(["CZ", "EU"]);
  const [loading, setLoading] = useState(false);
  const [res, setRes] = useState<RetrieveResponse | null>(null);
  const [ans, setAns] = useState<AnswerResponse | null>(null);
  // The EXACT query that produced `ans` — the clarify follow-up re-POSTs THIS verbatim.
  const [askedQuery, setAskedQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [corpus, setCorpus] = useState<CorpusOverview | null>(null);

  useEffect(() => {
    api.corpusOverview().then(setCorpus).catch(() => setCorpus(null));
  }, []);

  async function runSearch(q: string) {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setRes(null);
    setAns(null);
    try {
      setRes(await api.retrieve({ query: q, jurisdiction: jur, top_n_rerank: 10, expand_top_n: 0 }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  // The synthesized-answer path (the plain-language analysis). Primary when ANSWER_PRIMARY is on.
  async function ask(q: string) {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setRes(null);
    setAns(null);
    setAskedQuery(q);
    try {
      setAns(await api.answer(q, {}));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  // The primary action: analysis when the flag is on, else the legacy provisions search.
  const runPrimary = ANSWER_PRIMARY ? ask : runSearch;

  function askExample(q: string) {
    setQuery(q);
    runPrimary(q);
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function toggleJur(j: string) {
    setJur((cur) => (cur.includes(j) ? cur.filter((x) => x !== j) : [...cur, j]));
  }

  // size per area, from the live corpus
  const sizeByExt = new Map((corpus?.acts ?? []).map((a) => [a.ext, a.provisions]));
  const areaSize = (exts: string[]) => exts.reduce((n, e) => n + (sizeByExt.get(e) ?? 0), 0);

  // The product console searches the app (regsuper) corpus → no `?corpus=` (undefined = app DB), so the
  // source-viewer deep-link is byte-identical to before. Kept as a single knob so a future pythia-corpus
  // search surface preserves the corpus the citation came from rather than silently falling back to app DB.
  const resultCorpus: string | undefined = undefined;
  const sourceHref = (provisionId: string) =>
    `/sources/${provisionId}${resultCorpus ? `?corpus=${encodeURIComponent(resultCorpus)}` : ""}`;

  return (
    <main className="mx-auto max-w-3xl px-4 py-12">
      <header className="mb-8">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900">Polarius</h1>
        <p className="mt-1 text-slate-500">
          {ANSWER_PRIMARY
            ? "Czech & EU law. Ask a question — get a plain-language analysis with every point linked to the exact provision."
            : "Czech & EU financial-regulation search. Ask a question — get the exact provisions that apply, each linked to its source."}
        </p>
      </header>

      {/* QuickAsk — retrieval-first */}
      <form onSubmit={(e) => { e.preventDefault(); runPrimary(query); }} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <textarea
          className="h-24 w-full resize-none rounded-lg border border-slate-200 bg-white px-3 py-2 text-[15px] text-slate-900 placeholder:text-slate-400 outline-none focus:border-slate-400"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. What are a fund depositary's duties when marketing an AIF to retail investors?"
        />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <div className="flex gap-1">
            {["CZ", "EU"].map((j) => (
              <button key={j} type="button" onClick={() => toggleJur(j)} className={`rounded-full px-3 py-1 text-[13px] ${jur.includes(j) ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600"}`}>{j}</button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {ANSWER_PRIMARY && (
              <button type="button" onClick={() => runSearch(query)} disabled={loading || !query.trim()} className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-600 hover:border-slate-400 disabled:opacity-50">
                Just the provisions
              </button>
            )}
            <button type="submit" disabled={loading || !query.trim()} className="rounded-lg bg-slate-900 px-5 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50">
              {loading ? (ANSWER_PRIMARY ? "Analyzing…" : "Searching…") : ANSWER_PRIMARY ? "Get analysis →" : "Find provisions →"}
            </button>
          </div>
        </div>
      </form>

      <p className="mt-3 text-center text-[13px] text-slate-400">
        For a full compliance analysis of a project,{" "}
        <Link href="/projects/new" className="text-slate-600 underline">start a project →</Link>
      </p>

      {error && <div className="mt-6 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div>}

      {ans && (
        <div className="mt-6">
          <AnswerView answer={ans} query={askedQuery} onAnswer={setAns} />
        </div>
      )}

      {res?.refused && (
        <div className="mt-6 rounded-lg border-2 border-amber-300 bg-amber-50 p-4">
          <h2 className="font-semibold text-amber-900">Outside the loaded regulations</h2>
          <p className="mt-1 text-sm text-amber-800">{res.reason ?? "The question fell below the confidence threshold — no provision is returned rather than a wrong one."}</p>
        </div>
      )}

      {res && res.flagged && !res.refused && (
        <div className="mt-6 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          Low confidence — these results are shown but fell below the calibrated threshold. Treat them as a starting point and verify against the cited rule.
        </div>
      )}

      {res && !res.refused && (
        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between text-[13px] text-slate-500">
            <span>{res.results.length} provisions · ranked by relevance</span>
            <span>{res.diagnostics?.latency_ms != null ? `${res.diagnostics.latency_ms} ms` : ""}</span>
          </div>
          <ol className="space-y-3">
            {res.results.map((r) => (
              <li key={r.provision_id}>
                <Link href={sourceHref(r.provision_id)} className="block rounded-lg border border-slate-200 bg-white p-4 transition-colors hover:border-slate-300">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-slate-900">{r.label ?? "(provision)"}</span>
                    {r.legal_hierarchy && <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">{HIER_LABEL[r.legal_hierarchy] ?? r.legal_hierarchy}</span>}
                    {r.jurisdiction && <span className="text-[11px] text-slate-400">{r.jurisdiction}</span>}
                  </div>
                  {r.title && <p className="mt-1 text-[13px] text-slate-500">{r.title}{r.external_id ? ` · ${r.external_id}` : ""}</p>}
                  <div className="mt-2 flex items-center gap-3 text-[11px] text-slate-400">
                    <span>rank #{r.rank}</span>
                    {r.rerank_score != null && <span>relevance {r.rerank_score.toFixed(2)}</span>}
                  </div>
                </Link>
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* WHAT'S LOADED — the plain-language guide (shown until the user searches) */}
      {!res && !ans && (
        <section className="mt-10">
          <h2 className="text-lg font-semibold text-slate-900">What you can ask about</h2>
          <p className="mt-1 text-sm text-slate-500">
            Polarius has loaded {corpus ? `${corpus.acts.length} Czech & EU laws (${corpus.provisions.toLocaleString()} provisions)` : "the Czech & EU laws"} covering these areas. Click an example to try it — every answer cites the exact rule.
          </p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {AREAS.map((a) => {
              const size = areaSize(a.exts);
              if (corpus && size === 0) return null; // hide an area whose acts aren't actually loaded
              return (
                <div key={a.key} className="rounded-xl border border-slate-200 bg-white p-4">
                  <div className="flex items-baseline justify-between">
                    <h3 className="font-medium text-slate-900">{a.name}</h3>
                    {size > 0 && <span className="text-[11px] text-slate-400">{size.toLocaleString()} rules</span>}
                  </div>
                  <p className="mt-1 text-[13px] text-slate-500">{a.blurb}</p>
                  <button onClick={() => askExample(a.example)} className="mt-3 text-left text-[13px] text-slate-700 hover:text-slate-900">
                    <span className="text-slate-400">Try: </span>&ldquo;{a.example}&rdquo; →
                  </button>
                </div>
              );
            })}
          </div>
          {corpus && (
            <details className="mt-5">
              <summary className="cursor-pointer text-[13px] text-slate-500 hover:text-slate-700">See the full list of loaded laws ({corpus.acts.length})</summary>
              <ul className="mt-2 space-y-1 text-[13px] text-slate-500">
                {corpus.acts.map((a) => (
                  <li key={a.ext} className="flex items-center justify-between gap-3 border-b border-slate-100 py-1">
                    <span className="truncate">{a.title}</span>
                    <span className="shrink-0 tabular-nums text-slate-400">{a.jurisdiction} · {a.provisions.toLocaleString()}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      <footer className="mt-12 border-t border-slate-100 pt-4 text-center text-[12px] text-slate-400">
        {ANSWER_VIEW_ENABLED && (
          <>
            <Link href="/answer" className="hover:text-slate-600">ask a legal question</Link>
            {" · "}
          </>
        )}
        {DIGEST_UI_ENABLED && (
          <>
            <Link href="/profiles" className="hover:text-slate-600">compliance for your business</Link>
            {" · "}
          </>
        )}
        <Link href="/retrieve" className="hover:text-slate-600">advanced retrieval</Link>
        {" · "}
        <Link href="/control" className="hover:text-slate-600">control room ↗</Link>
      </footer>
    </main>
  );
}
