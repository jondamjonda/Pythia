// NonAnswerFrame (M8) — the four HONEST non-answer frames. None of these ever
// renders a synthesized "answer"; each renders only the server-supplied honest
// copy (escaped text) plus, where the engine grounded them, REAL citation chips.
//
//   closest      → not_in_corpus: "nejbližší související předpisy (nejsou přímo
//                  k věci)" + meta.closest_provisions as chips, NO prose.
//   notlegal     → not_a_legal_question: honest non-legal envelope, no citations.
//   falsepremise → "Pozor: váš dotaz předpokládá …" correction banner + the
//                  grounded corrected mini-answer (meta.correction +
//                  meta.correcting_citations).
//   refuse       → honest refusal envelope (post-generation verify net).
//
// Copy is whatever the API supplies (title/body/reason); we never invent legal
// text. Chips are escaped + click through via ChipResolver.
import { ChipRow } from "@/components/answer/ChipResolver";
import type { AnswerResponse } from "@/lib/api";

function Frame({
  tone,
  title,
  children,
}: {
  tone: "neutral" | "warn" | "info";
  title: string;
  children?: React.ReactNode;
}) {
  const cls =
    tone === "warn"
      ? "border-amber-300 bg-amber-50"
      : tone === "info"
        ? "border-sky-200 bg-sky-50"
        : "border-slate-200 bg-white";
  return (
    <div className={`mt-6 rounded-xl border ${cls} p-5`}>
      <h2 className="text-base font-semibold text-slate-900">{title}</h2>
      {children}
    </div>
  );
}

export function NonAnswerFrame({ answer }: { answer: AnswerResponse }) {
  const meta = answer.meta ?? {};
  // Body text the UI can show: prefer the server `body`, else `reason`. Both are
  // escaped strings; never HTML.
  const body = (answer.body ?? answer.reason ?? "").trim();

  switch (answer.render_mode) {
    case "closest": {
      const closest = meta.closest_provisions ?? [];
      return (
        <Frame
          tone="info"
          title={answer.title ?? "Nejbližší související předpisy (nejsou přímo k věci)"}
        >
          {body && <p className="mt-2 text-sm text-slate-700 whitespace-pre-wrap">{body}</p>}
          {closest.length > 0 ? (
            <ChipRow chips={closest} />
          ) : (
            <p className="mt-2 text-sm text-slate-500">
              Nenašli jsme žádné přímo související předpisy.
            </p>
          )}
        </Frame>
      );
    }

    case "notlegal":
      return (
        <Frame tone="neutral" title={answer.title ?? "Toto nevypadá jako právní dotaz"}>
          <p className="mt-2 text-sm text-slate-700 whitespace-pre-wrap">
            {body ||
              "Zkuste prosím dotaz přeformulovat jako konkrétní právní otázku k českým či EU předpisům."}
          </p>
        </Frame>
      );

    case "falsepremise": {
      const correction = (meta.correction ?? "").trim();
      const correctingChips = meta.correcting_citations ?? [];
      return (
        <Frame tone="warn" title={answer.title ?? "Pozor: dotaz obsahuje nesprávný předpoklad"}>
          {body && <p className="mt-2 text-sm text-slate-800 whitespace-pre-wrap">{body}</p>}
          {correction && (
            <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
              <p className="text-sm text-slate-800 whitespace-pre-wrap">{correction}</p>
              <ChipRow chips={correctingChips} />
            </div>
          )}
        </Frame>
      );
    }

    case "refuse":
    default:
      return (
        <Frame tone="neutral" title={answer.title ?? "Na tento dotaz nemůžeme spolehlivě odpovědět"}>
          <p className="mt-2 text-sm text-slate-700 whitespace-pre-wrap">
            {body ||
              "Odpověď byla zadržena, protože ji nelze doložit citacemi z načtených předpisů."}
          </p>
        </Frame>
      );
  }
}
