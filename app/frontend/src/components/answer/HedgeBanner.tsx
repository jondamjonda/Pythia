// HedgeBanner (M8) — the amber "lower confidence, verify with a professional"
// banner shown above a `hedged` answer. The copy comes from the server
// (`hedge_banner`/`hedge`, sourced from the engine bands.py::_HEDGE_COPY, which is
// lawyer-reviewed under G14); this component only renders whatever string it is
// given (escaped text), with a sane CZ fallback so the banner is never blank when
// the mode is hedged. It never invents legal substance.
export function HedgeBanner({ text }: { text?: string | null }) {
  const copy =
    text && text.trim()
      ? text
      : "Nižší jistota — ověřte u odborníka.";
  return (
    <div
      role="note"
      className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
    >
      <span className="font-medium">⚠ </span>
      {copy}
    </div>
  );
}
