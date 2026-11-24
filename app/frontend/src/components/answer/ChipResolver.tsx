// ChipResolver (M8) — turns an answer-surface citation chip into a clickable
// deep-link into the source viewer. Thin wrapper over the existing CitationLink:
// it pins the corpus to "pythia" (the answer pipeline grounds on the pythia
// corpus) unless the server explicitly stamped a different corpus on the chip,
// so the M7 viewer reads the corpus the citation actually came from (and falls
// back to the regsuper id-map for un-cutover domains — never a 404 across the seam).
//
// The label is model/data-derived → React escapes it (CitationLink renders it as
// text inside JSX); it is NEVER injected as HTML.
import { CitationLink } from "@/components/CitationLink";
import type { AnswerChip } from "@/lib/api";

export function ChipResolver({ chip }: { chip: AnswerChip }) {
  // Pin to the corpus the answer is grounded on; honour an explicit non-empty
  // server stamp if present (future-proofs a cross-corpus answer).
  const corpus = chip.corpus && chip.corpus.trim() ? chip.corpus : "pythia";
  return (
    <CitationLink
      provisionId={chip.provision_id}
      label={chip.label ?? undefined}
      corpus={corpus}
    />
  );
}

// A simple inline row of chips (used by sections and the closest/falsepremise
// frames). Renders nothing when there are no chips.
export function ChipRow({ chips }: { chips?: AnswerChip[] | null }) {
  if (!chips || chips.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {chips.map((c, i) => (
        <ChipResolver key={`${c.provision_id}-${i}`} chip={c} />
      ))}
    </div>
  );
}
