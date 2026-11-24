// CitationLink — every finding carries citations; the lawyer clicks one to
// open the source viewer. Plan §13.5 Day 14 calls these out as "always
// visible". We render a tight monospace pill so it visually stands out
// from prose.
import Link from "next/link";

export function CitationLink({
  provisionId,
  label,
  corpus,
}: {
  provisionId: string;
  label?: string;
  // Optional corpus selector carried into the source viewer (?corpus=). Omit (undefined) → the app DB,
  // identical to before. Pass "pythia" so a graph-corpus citation deep-links into the corpus it came from.
  corpus?: string;
}) {
  const href = `/sources/${provisionId}${corpus ? `?corpus=${encodeURIComponent(corpus)}` : ""}`;
  return (
    <Link
      href={href}
      className="inline-flex items-center rounded border border-slate-300 bg-white px-1.5 py-0.5 font-mono text-xs text-slate-700 hover:border-slate-500 hover:bg-slate-50"
      title="Open source provision"
    >
      📜 {label ?? provisionId.slice(0, 8)}
    </Link>
  );
}
