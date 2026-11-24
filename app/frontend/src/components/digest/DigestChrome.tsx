// DigestChrome (LEDGER-M9) — the shared honesty furniture every digest view
// must show: the lawyer-supplied disclaimer (rendered verbatim, escaped text)
// and the has_unreviewed_citations banner. Both are server-supplied; the UI
// never invents the disclaimer copy.

// The unreviewed-citations warning — shown when ANY response in the digest
// carries findings whose citations have not yet been human-reviewed.
export function UnreviewedCitationsBanner({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
      Některé citace v tomto přehledu zatím nebyly ověřeny právníkem. Berte je
      jako východisko a ověřte je proti uvedenému předpisu.
    </div>
  );
}

// The disclaimer — rendered verbatim as escaped text (the server supplies the
// §2.2.4 Czech text; we never fabricate legal copy). Renders nothing when empty.
export function DigestDisclaimer({ text }: { text?: string }) {
  const t = (text ?? "").trim();
  if (!t) return null;
  return (
    <p className="border-t border-slate-100 pt-4 text-[12px] leading-relaxed text-slate-400 whitespace-pre-wrap">
      {t}
    </p>
  );
}
