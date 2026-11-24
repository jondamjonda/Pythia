// AnswerView (M8) — the six-render_mode switch for the consumer answer surface.
//
// full / hedged → the (up to three) CZ answer sections + inline clickable chips;
//                 `hedged` prepends the amber low-confidence HedgeBanner.
// closest / notlegal / falsepremise / refuse → the honest NonAnswerFrame (never a
//                 fabricated answer).
//
// HARD INVARIANTS enforced here:
//  - XSS: all prose flows through AnswerSection (escaped inert text). No HTML injection.
//  - CASE-LAW HONESTY: the Judikatura (case_law) section is capability-flagged OFF
//    (CASE_LAW_ENABLED=false). The answer wire shape carries no per-ruling court +
//    case-number, so we cannot prove a ruling is court-backed → we SUPPRESS the
//    whole case-law section rather than risk an empty/court-less Judikatura panel.
//    No "[1167]" incoming-citation / headnote / apex widgets exist on this surface.
//  - NULL-WHEN-NULL: a missing band/meta/disclaimer renders nothing, never a fake badge.
import { AnswerSection } from "@/components/answer/AnswerSection";
import { AuthoritiesBlock } from "@/components/answer/AuthoritiesBlock";
import { ClarifyForm } from "@/components/answer/ClarifyForm";
import { HedgeBanner } from "@/components/answer/HedgeBanner";
import { NonAnswerFrame } from "@/components/answer/NonAnswerFrame";
import { PracticalPlan } from "@/components/answer/PracticalPlan";
import { UnderstandingPanel } from "@/components/answer/UnderstandingPanel";
import { CitationLink } from "@/components/CitationLink";
import type { AnswerChip, AnswerResponse, AnswerSection as Section } from "@/lib/api";

// Capability flag (N5 case-law honesty): the answer surface does NOT yet surface
// court-backed rulings, so the Judikatura section is never rendered. Flip ON only
// once a ruling carries a verified court + case-number per the case-law-honesty
// invariant. Kept as a const (not env) so the suppression is unconditional today.
const CASE_LAW_ENABLED = false;

function isCaseLaw(s: Section): boolean {
  return s.kind === "case_law";
}

function isAnswerMode(m: AnswerResponse["render_mode"]): boolean {
  return m === "full" || m === "hedged";
}

export function AnswerView({
  answer,
  profileId,
  query,
  asOf,
  onAnswer,
}: {
  answer: AnswerResponse;
  // T7 — optional profile context for the UNDERSTANDING PANEL's consented write-back.
  // Absent on the current answer surfaces → the write-back affordance is disabled with
  // an honest note (the save is deferred until a profile is linked).
  profileId?: string | null;
  // Track A / VL-A / E12 — the clarify FOLLOW-UP context. `query`/`asOf` are the ORIGINAL
  // ask (re-POSTed verbatim with the collected `slots`); `onAnswer` swaps the displayed
  // answer to the SECOND (sound) response, rendered in place. When absent (e.g. a caller
  // that does not wire the round-trip), a clarify frame still renders its questions +
  // disclaimer read-only — NEVER the refuse UI.
  query?: string | null;
  asOf?: string | null;
  onAnswer?: (next: AnswerResponse) => void;
}) {
  // Track A / VL-A / E12 — the STAGE-1 clarify frame. A clarify is refused=false: it is
  // NEITHER an answer NOR a non-answer, so it is routed to the slot-collection FORM
  // (render `questions`, resubmit `slots`), never to the honest non-answer / refuse UI.
  // This branch MUST precede the non-answer branch below.
  if (answer.render_mode === "clarify") {
    return (
      <div>
        <ClarifyForm
          answer={answer}
          query={query ?? ""}
          asOf={asOf}
          projectId={profileId}
          onAnswer={onAnswer ?? (() => {})}
        />
        <UnderstandingPanel answer={answer} profileId={profileId} />
      </div>
    );
  }

  // The four honest non-answer frames. The UNDERSTANDING PANEL still renders here (it
  // is orthogonal to whether we answered — a refusal still shows what we understood).
  if (!isAnswerMode(answer.render_mode)) {
    return (
      <div>
        <NonAnswerFrame answer={answer} />
        <UnderstandingPanel answer={answer} profileId={profileId} />
        <Disclaimer answer={answer} />
      </div>
    );
  }

  // full / hedged: render the sections, suppressing the capability-flagged-off
  // case-law section entirely (never an empty Judikatura panel).
  const sections = (answer.sections ?? []).filter(
    (s) => CASE_LAW_ENABLED || !isCaseLaw(s),
  );

  return (
    <div>
      {answer.render_mode === "hedged" && (
        <HedgeBanner text={answer.hedge_banner ?? answer.hedge} />
      )}

      {answer.title && (
        <h1 className="mt-4 text-xl font-semibold tracking-tight text-slate-900">
          {answer.title}
        </h1>
      )}

      {/* `body` is the optional lead/summary the server may supply (escaped text). */}
      {answer.body && answer.body.trim() && (
        <p className="mt-2 whitespace-pre-wrap text-[15px] leading-relaxed text-slate-800">
          {answer.body}
        </p>
      )}

      {sections.length > 0 ? (
        sections.map((s, i) => <AnswerSection key={`${s.kind}-${i}`} section={s} />)
      ) : (
        // An answer mode with no renderable sections: show the honest temporal/empty
        // note rather than a blank screen. Never fabricate content.
        <p className="mt-6 text-sm text-slate-500">
          {answer.as_of_unknown_note ??
            "Odpověď neobsahuje žádné doložitelné části."}
        </p>
      )}

      {/* B1c: the practical seven-slot action plan (obligations/deadlines, "Kam se
          obrátit", penalties, and DRAFT gap lines) — rendered UNDER the essay prose
          only when the payload carries a structured `practical` block. Absent → this
          renders nothing and the view above is byte-identical to today. */}
      <PracticalPlan answer={answer} />

      {/* W4 / T13: the "Kam se obrátit" competent-authority DIRECTORY (a NON-LEGAL
          practical aid). Rendered UNDER the practical plan and kept DISTINCT from the
          grounded-provisions/citation list below — it is never a citation chip. Absent/
          empty `authorities` → renders nothing (byte-identical). */}
      <AuthoritiesBlock answer={answer} />

      {/* The grounded, high-confidence provisions the analysis rests on — organized under the prose. */}
      {sections.length > 0 && (
        <GroundedProvisions sections={sections} flat={answer.chips} />
      )}

      {/* Temporal honesty: "datum účinnosti neuvedeno" rides as_of_unknown_note. */}
      {answer.as_of_unknown_note && sections.length > 0 && (
        <p className="mt-4 text-xs text-slate-400">{answer.as_of_unknown_note}</p>
      )}

      {/* T7: the UNDERSTANDING PANEL ("Jak jsme otázce porozuměli") — collapsed, rendered
          only when the payload carries a T7 `intake` block. Absent → renders nothing and the
          view above is byte-identical to today. */}
      <UnderstandingPanel answer={answer} profileId={profileId} />

      <Disclaimer answer={answer} />
    </div>
  );
}

// The grounded, high-confidence provisions the analysis is built on — deduped and grouped by act, shown as a
// compact organized "sources" list under the prose. These are the GROUNDED citations only (the engine VERIFY
// pass drops anything ungrounded), so every entry is high-confidence; the broader retrieved-but-uncited set is
// deliberately NOT listed. Renders nothing when there are no grounded chips.
function GroundedProvisions({ sections, flat }: { sections: Section[]; flat?: AnswerChip[] | null }) {
  // Union of every section's chips + the flat grounded set, deduped by provision_id (first wins → keeps the
  // citation order the analysis used).
  const all: AnswerChip[] = [...sections.flatMap((s) => s.chips ?? []), ...(flat ?? [])];
  const seen = new Set<string>();
  const order: string[] = [];
  const byAct = new Map<string, { provision_id: string; section: string; corpus?: string | null }[]>();
  for (const c of all) {
    if (!c.provision_id || seen.has(c.provision_id)) continue;
    seen.add(c.provision_id);
    const { act, section } = splitLabel(c.label);
    if (!byAct.has(act)) {
      byAct.set(act, []);
      order.push(act);
    }
    byAct.get(act)!.push({ provision_id: c.provision_id, section, corpus: c.corpus });
  }
  if (order.length === 0) return null;
  return (
    <section className="mt-8 border-t border-slate-100 pt-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Podkladová ustanovení
      </h2>
      <p className="mt-1 text-xs text-slate-400">
        Ustanovení s vysokou shodou, o která se analýza opírá — klikněte pro otevření zdroje.
      </p>
      <div className="mt-3 space-y-3">
        {order.map((act) => (
          <div key={act}>
            <div className="text-[13px] font-medium text-slate-700">{act}</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {byAct.get(act)!.map((it) => (
                <CitationLink
                  key={it.provision_id}
                  provisionId={it.provision_id}
                  label={it.section}
                  corpus={it.corpus ?? "pythia"}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// "[455/1991 § 5]" → { act: "455/1991", section: "§ 5" }; "[32024R1624 Article 10(1)]" → act/Article. Strip
// brackets; the first whitespace-delimited token is the act external_id, the remainder is the provision label.
function splitLabel(label?: string | null): { act: string; section: string } {
  const clean = (label ?? "").replace(/^\[/, "").replace(/\]$/, "").trim();
  const m = clean.match(/^(\S+)\s+(.*)$/);
  if (m) return { act: m[1], section: m[2] };
  return { act: clean || "—", section: clean || "—" };
}

// The legal disclaimer (server-supplied, escaped). NULL-when-NULL: absent → nothing.
function Disclaimer({ answer }: { answer: AnswerResponse }) {
  if (!answer.disclaimer || !answer.disclaimer.trim()) return null;
  return (
    <p className="mt-6 border-t border-slate-100 pt-4 text-xs leading-relaxed text-slate-400">
      {answer.disclaimer}
    </p>
  );
}
