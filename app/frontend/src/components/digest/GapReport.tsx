// GapReport (LEDGER-M9) — the compliance gaps, critical-first. Two visible
// sections so a demoted gap is NEVER dropped:
//   1. gaps — the live gaps (severity: kritické | důležité | doporučené)
//   2. context_gaps — WF-1 Inc5 / WF-4 / WF-6 demotions (out_of_scope /
//      duplicate / stale), each carrying its suppressed_by reason(s).
//
// All strings are data-derived → React text (escaped). Chips via CitationLink
// (regsuper corpus, no ?corpus param).
import { CitationLink } from "@/components/CitationLink";
import type { GapItem } from "@/lib/api";

const SEVERITY_CLS: Record<string, string> = {
  kritické: "bg-rose-50 text-rose-800 ring-rose-200",
  důležité: "bg-amber-50 text-amber-800 ring-amber-200",
  doporučené: "bg-sky-50 text-sky-800 ring-sky-200",
};

function GapRow({ g, demoted }: { g: GapItem; demoted?: boolean }) {
  const cls = SEVERITY_CLS[g.severity_label] ?? "bg-slate-100 text-slate-600 ring-slate-200";
  return (
    <li className={`rounded-lg border border-slate-200 bg-white p-4 ${demoted ? "opacity-75" : ""}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-slate-900">{g.statement}</span>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${cls}`}
        >
          {g.severity_label}
        </span>
      </div>
      {/* demotion reason(s) — only present on context_gaps */}
      {demoted && g.suppressed_by && g.suppressed_by.length > 0 && (
        <p className="mt-1 text-[13px] text-slate-500">
          Důvod přeřazení: {g.suppressed_by.join(", ")}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-slate-400">
        <span>{g.legal_source}</span>
        <CitationLink provisionId={g.cited_provision_id} label={g.legal_source} />
      </div>
    </li>
  );
}

export function GapReport({
  gaps,
  contextGaps,
}: {
  gaps: GapItem[];
  contextGaps: GapItem[];
}) {
  return (
    <section>
      <h2 className="text-lg font-semibold text-slate-900">Mezery v souladu</h2>
      {gaps.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">Nebyly zjištěny žádné mezery.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {gaps.map((g, i) => (
            <GapRow key={`${g.cited_provision_id}-${i}`} g={g} />
          ))}
        </ul>
      )}

      {/* Demoted gaps — own visible section, never dropped. */}
      {contextGaps.length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Kontext ({contextGaps.length})
          </h3>
          <p className="mt-1 text-[13px] text-slate-500">
            Tyto mezery byly přeřazeny do kontextu (nevztahují se, jsou duplicitní
            nebo zastaralé) — uvádíme je pro úplnost.
          </p>
          <ul className="mt-2 space-y-2">
            {contextGaps.map((g, i) => (
              <GapRow key={`ctx-${g.cited_provision_id}-${i}`} g={g} demoted />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
