// FindingCard — one applicable/obligation/gap finding with citations.
import type { Finding } from "@/lib/api";
import { CitationLink } from "./CitationLink";
import { ConfidenceBadge } from "./ConfidenceBadge";

const TYPE_STYLES: Record<string, { ring: string; tag: string; label: string }> = {
  applicable: {
    ring: "ring-sky-200",
    tag: "bg-sky-50 text-sky-800",
    label: "Applicable",
  },
  obligation: {
    ring: "ring-emerald-200",
    tag: "bg-emerald-50 text-emerald-800",
    label: "Obligation",
  },
  gap: {
    ring: "ring-rose-200",
    tag: "bg-rose-50 text-rose-800",
    label: "Gap",
  },
  // B2b-13 — a cited below-floor verdict the cascade was NOT confident enough to assert. It MUST read
  // as "needs review", never as a confident finding (§16.3 lawful lane).
  refusal: {
    ring: "ring-amber-200",
    tag: "bg-amber-50 text-amber-800",
    label: "Needs review",
  },
};

// B2b-13 — the fallback for an UNKNOWN finding_type. Critically NOT "Applicable": a type we don't
// recognise must never render as a confident applicable finding (that false-confidence is exactly what
// the lawful lane exists to prevent). Neutral, honest, visibly "unknown".
const UNKNOWN_STYLE = {
  ring: "ring-slate-200",
  tag: "bg-slate-100 text-slate-700",
  label: "Unknown",
};

// A refusal/needs-review verdict is not a graded finding — a confidence % would re-introduce the
// false-confidence we just removed, so we surface the review state instead.
const NON_CONFIDENT_TYPES = new Set(["refusal"]);

// Human-readable review state, shown when a finding is not a settled "reviewed" result.
const REVIEW_STATE_LABELS: Record<string, string> = {
  review_requested: "Review requested",
  needs_quote: "Needs source quote",
  unverified_citation: "Unverified citation",
};

export function FindingCard({ finding }: { finding: Finding }) {
  const s = TYPE_STYLES[finding.finding_type] ?? UNKNOWN_STYLE;
  const isNonConfident = NON_CONFIDENT_TYPES.has(finding.finding_type);
  const payload = (finding.payload ?? {}) as Record<string, unknown>;

  // Per-type details we want to surface concisely.
  const details = renderDetails(finding.finding_type, payload);
  const reviewLabel = REVIEW_STATE_LABELS[finding.review_state];

  return (
    <article className={`rounded-lg border bg-white p-4 shadow-sm ring-1 ${s.ring}`}>
      <header className="mb-2 flex items-center gap-2">
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${s.tag}`}>{s.label}</span>
        {/* Refusals/unknowns carry no trustworthy confidence score — show the review state instead. */}
        {isNonConfident ? (
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">
            {reviewLabel ?? "Awaiting human review"}
          </span>
        ) : (
          <>
            <ConfidenceBadge score={finding.confidence_overall} />
            {reviewLabel && (
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">
                {reviewLabel}
              </span>
            )}
          </>
        )}
        <span className="ml-auto text-xs text-slate-400">
          {finding.analysis_layer} · {finding.generated_by}
        </span>
      </header>

      <p className="mb-2 text-sm leading-relaxed text-slate-800">{finding.statement}</p>

      {details && <div className="mb-3 text-xs text-slate-600">{details}</div>}

      <footer className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-slate-500">Citations:</span>
        {finding.cited_provisions.map((pid) => (
          <CitationLink key={pid} provisionId={pid} />
        ))}
      </footer>
    </article>
  );
}

function renderDetails(kind: string, p: Record<string, unknown>) {
  if (kind === "obligation") {
    const modal = p["modal"] as string | undefined;
    const actor = p["actor"] as string | undefined;
    const deadline = p["deadline"] as string | undefined;
    const conditions = (p["conditions"] as string[] | undefined) ?? [];
    return (
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        {actor && (
          <div>
            <span className="font-medium text-slate-700">Actor:</span> {actor}
          </div>
        )}
        {modal && (
          <div>
            <span className="font-medium text-slate-700">Modal:</span> {modal}
          </div>
        )}
        {deadline && (
          <div>
            <span className="font-medium text-slate-700">Deadline:</span> {deadline}
          </div>
        )}
        {conditions.length > 0 && (
          <div className="col-span-2">
            <span className="font-medium text-slate-700">Conditions:</span>{" "}
            {conditions.slice(0, 3).join(" · ")}
            {conditions.length > 3 ? ` (+${conditions.length - 3})` : ""}
          </div>
        )}
      </div>
    );
  }
  if (kind === "gap") {
    const missing = (p["missing_evidence"] as Array<{ evidence_type: string }> | undefined) ?? [];
    const obligation_action = p["obligation_action"] as string | undefined;
    return (
      <div className="space-y-1">
        <div>
          <span className="font-medium text-slate-700">Missing evidence:</span>{" "}
          {missing.map((m) => m.evidence_type).join(", ") || "—"}
        </div>
        {obligation_action && (
          <div className="text-slate-500">
            <span className="font-medium text-slate-700">Obligation:</span> {obligation_action}
          </div>
        )}
      </div>
    );
  }
  if (kind === "applicable") {
    const reasoning = p["reasoning"] as string | undefined;
    if (reasoning) {
      return (
        <div className="text-slate-500">
          <span className="font-medium text-slate-700">Reasoning:</span> {reasoning}
        </div>
      );
    }
  }
  if (kind === "refusal") {
    // Surface WHY this verdict was withheld (the cascade records a reason), so a reviewer sees the
    // honest "we weren't confident enough" rather than nothing. Falls back to the statement above.
    const reason = (p["reason"] ?? p["refusal_reason"]) as string | undefined;
    if (reason) {
      return (
        <div className="text-amber-700">
          <span className="font-medium">Why this needs review:</span> {reason}
        </div>
      );
    }
  }
  return null;
}
