// ComplianceChecklist (LEDGER-M9) — each obligation paired with its compliance
// status (CZ literals: splněno | nesplněno | neověřeno | nejasné). Read-only:
// the status comes from the server, the FE never asserts a status the data
// doesn't carry.
//
// All strings are data-derived → rendered as React text (escaped). Chips
// deep-link via CitationLink (regsuper corpus, no ?corpus param).
import { CitationLink } from "@/components/CitationLink";
import type { ChecklistItem } from "@/lib/api";

const STATUS_CLS: Record<string, string> = {
  splněno: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  nesplněno: "bg-rose-50 text-rose-800 ring-rose-200",
  neověřeno: "bg-slate-100 text-slate-600 ring-slate-200",
  nejasné: "bg-amber-50 text-amber-800 ring-amber-200",
};

export function ComplianceChecklist({ items }: { items: ChecklistItem[] }) {
  return (
    <section>
      <h2 className="text-lg font-semibold text-slate-900">Kontrolní seznam</h2>
      {items.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">Žádné položky ke kontrole.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {items.map((it, i) => {
            const o = it.obligation;
            // Unknown status → neutral ring, raw label (never a guessed status).
            const cls = STATUS_CLS[it.status] ?? "bg-slate-100 text-slate-600 ring-slate-200";
            return (
              <li
                key={`${o.cited_provision_id}-${i}`}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium text-slate-900">
                    {o.modal_label} — {o.actor_label}
                  </span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${cls}`}
                  >
                    {it.status}
                  </span>
                </div>
                {o.deadline && (
                  <p className="mt-1 text-[13px] text-slate-500">Termín: {o.deadline}</p>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-slate-400">
                  <span>{o.legal_source}</span>
                  <CitationLink provisionId={o.cited_provision_id} label={o.legal_source} />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
