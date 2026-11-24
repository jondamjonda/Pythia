// Maps a 0..1 confidence to {green, amber, red} for the ConfidenceBadge.
// Thresholds are intentionally conservative for legal review — anything
// below 0.65 lands in the red bucket so the lawyer sees a clear "needs
// review" signal.

export type ConfidenceLevel = "high" | "medium" | "low";

export function confidenceLevel(score: number): ConfidenceLevel {
  if (score >= 0.85) return "high";
  if (score >= 0.65) return "medium";
  return "low";
}

export const CONFIDENCE_STYLES: Record<ConfidenceLevel, { bg: string; text: string; label: string }> = {
  high: { bg: "bg-emerald-100", text: "text-emerald-800", label: "High" },
  medium: { bg: "bg-amber-100", text: "text-amber-800", label: "Medium" },
  low: { bg: "bg-rose-100", text: "text-rose-800", label: "Low" },
};
