// ActionList (LEDGER-M9) — the prioritised next-steps list (CZ priority
// literals: okamžitě | do 30 dnů | do 90 dnů | průběžně). Read-only.
//
// All strings are data-derived → React text (escaped). No chips (the wire model
// carries only legal_source as text, no cited_provision_id on an action).
import type { ActionItem } from "@/lib/api";

const PRIORITY_CLS: Record<string, string> = {
  okamžitě: "bg-rose-50 text-rose-800 ring-rose-200",
  "do 30 dnů": "bg-amber-50 text-amber-800 ring-amber-200",
  "do 90 dnů": "bg-sky-50 text-sky-800 ring-sky-200",
  průběžně: "bg-slate-100 text-slate-600 ring-slate-200",
};

export function ActionList({ actions }: { actions: ActionItem[] }) {
  return (
    <section>
      <h2 className="text-lg font-semibold text-slate-900">Doporučené kroky</h2>
      {actions.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">Žádné doporučené kroky.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {actions.map((a, i) => {
            const cls = PRIORITY_CLS[a.priority] ?? "bg-slate-100 text-slate-600 ring-slate-200";
            return (
              <li
                key={`${i}-${a.statement.slice(0, 16)}`}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-slate-900">{a.statement}</span>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${cls}`}
                  >
                    {a.priority}
                  </span>
                </div>
                {a.legal_source && (
                  <p className="mt-1 text-[12px] text-slate-400">{a.legal_source}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
