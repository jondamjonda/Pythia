// ObligationRegister (LEDGER-M9) — the plain-language register of duties that
// bind the SME. Two visible sections:
//   1. the binding register (binds 'yes' / 'conditional')
//   2. "does_not_bind" — WF-1 out_of_scope obligations DEMOTED here. These are
//      surfaced for transparency in their OWN visible section, NEVER dropped.
//
// NULL-WHEN-NULL: deadline / legal_source_url / the conditional reason render
// only when present — we never fabricate a date or a "binds you" claim. All
// strings are server/data-derived → rendered as React text (escaped), never HTML.
// Citation chips deep-link via CitationLink with NO ?corpus (regsuper corpus).
import { CitationLink } from "@/components/CitationLink";
import type { ObligationItem } from "@/lib/api";

const BINDS_LABEL: Record<string, { label: string; cls: string }> = {
  conditional: {
    label: "Platí podmíněně",
    cls: "bg-amber-50 text-amber-800 ring-amber-200",
  },
  out_of_scope: {
    label: "Nevztahuje se na vás",
    cls: "bg-slate-100 text-slate-600 ring-slate-200",
  },
};

function ObligationRow({ o }: { o: ObligationItem }) {
  const binds = o.binds && o.binds !== "yes" ? BINDS_LABEL[o.binds] : undefined;
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-medium text-slate-900">{o.modal_label}</span>
        <span className="text-slate-600">— {o.actor_label}</span>
        {binds && (
          <span
            className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${binds.cls}`}
          >
            {binds.label}
          </span>
        )}
      </div>
      {/* deadline: NULL-when-NULL — render nothing when absent */}
      {o.deadline && (
        <p className="mt-1 text-[13px] text-slate-500">Termín: {o.deadline}</p>
      )}
      {/* conditional reason — only when the server supplied one */}
      {o.binds === "conditional" && o.binds_reason && (
        <p className="mt-1 text-[13px] text-amber-700">{o.binds_reason}</p>
      )}
      {o.binds === "out_of_scope" && o.binds_reason && (
        <p className="mt-1 text-[13px] text-slate-500">{o.binds_reason}</p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-slate-400">
        <span>{o.legal_source}</span>
        <CitationLink provisionId={o.cited_provision_id} label={o.legal_source} />
        {o.review_state && (
          <span className="text-slate-400">· {o.review_state}</span>
        )}
      </div>
    </li>
  );
}

export function ObligationRegister({
  obligations,
  doesNotBind,
}: {
  obligations: ObligationItem[];
  doesNotBind: ObligationItem[];
}) {
  return (
    <section>
      <h2 className="text-lg font-semibold text-slate-900">Vaše povinnosti</h2>
      {obligations.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">
          Pro tento profil nebyly nalezeny žádné závazné povinnosti.
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {obligations.map((o, i) => (
            <ObligationRow key={`${o.cited_provision_id}-${i}`} o={o} />
          ))}
        </ul>
      )}

      {/* The demoted set — its OWN visible section, never dropped (WF-1). */}
      {doesNotBind.length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Nevztahuje se na vás ({doesNotBind.length})
          </h3>
          <p className="mt-1 text-[13px] text-slate-500">
            Tyto povinnosti se podle vašeho profilu na vás nevztahují — uvádíme je
            pro úplnost.
          </p>
          <ul className="mt-2 space-y-2 opacity-75">
            {doesNotBind.map((o, i) => (
              <ObligationRow key={`oos-${o.cited_provision_id}-${i}`} o={o} />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
