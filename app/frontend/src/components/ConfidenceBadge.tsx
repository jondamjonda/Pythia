// Confidence pill — green/amber/red per plan §13.5 Day 14.
import { CONFIDENCE_STYLES, confidenceLevel } from "@/lib/confidence";

export function ConfidenceBadge({ score }: { score: number }) {
  const level = confidenceLevel(score);
  const s = CONFIDENCE_STYLES[level];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${s.bg} ${s.text}`}
      title={`Confidence ${(score * 100).toFixed(0)}%`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {s.label} {(score * 100).toFixed(0)}%
    </span>
  );
}
