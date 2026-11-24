// AuthoritiesBlock (W4 / T13) — the "Kam se obrátit" competent-authority directory.
//
// Renders the CONCRETE competent office(s) the resolver surfaced for the practical
// "kam se obrátit / where do I go" slot, rendered ONLY when the answer payload
// carries a non-empty `authorities` list (server flag `authority_registry_enabled`
// ON + a known municipality/topic resolved an office). Absent/empty → this renders
// NOTHING (no header, no placeholder) and the view above is byte-identical.
//
// HARD INVARIANTS:
//  - NON-LEGAL DIRECTORY AID, NOT LEGAL CONTENT. This block is a practical pointer to
//    an office; it grounds no claim. It MUST NEVER be rendered inside a citation chip
//    or the "Podkladová ustanovení" (grounded provisions) list, and it carries a
//    clear non-legal label so a reader never mistakes it for a cited legal source.
//  - DRAFT HONESTY (G14): every entry is DRAFT / advocate-review-pending. The seed is
//    machine-derived, so a subtle DRAFT badge rides the block; we never assert the
//    office as verified.
//  - XSS / OUTPUT-SAFETY: every server string (name, source URL host, as-of date) is
//    rendered as React text children only → HTML-escaped, inert. The source link uses
//    rel="noopener noreferrer" and only renders for an http(s) URL.
//  - UNKNOWN-SHAPE SAFE: an entry missing a name is dropped; a missing source/date
//    simply omits that line. Nothing here is fabricated.
import type { AnswerResponse, Authority } from "@/lib/api";

// match_kind → the human descriptor of HOW the office was matched. Kept as fixed UI
// chrome (never server copy). Unknown kinds fall through to no descriptor.
const MATCH_KIND_LABEL: Record<string, string> = {
  national: "celostátně příslušný úřad",
  okres: "místně příslušný (okres)",
  orp: "místně příslušný (ORP)",
  kraj: "místně příslušný (kraj)",
};

function matchKindLabel(kind?: string | null): string | null {
  if (!kind) return null;
  return MATCH_KIND_LABEL[kind.trim()] ?? null;
}

// Only render a source link for a real http(s) URL (never a javascript:/data: URI).
function safeHttpUrl(raw?: string | null): string | null {
  const v = (raw ?? "").trim();
  if (!v) return null;
  try {
    const u = new URL(v);
    if (u.protocol === "http:" || u.protocol === "https:") return u.toString();
  } catch {
    return null;
  }
  return null;
}

// The visible label for a source link: the host (never the raw full URL).
function sourceHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function AuthorityRow({ authority }: { authority: Authority }) {
  const name = (authority.name ?? "").trim();
  if (!name) return null;
  const descriptor = matchKindLabel(authority.match_kind);
  const src = safeHttpUrl(authority.source_uri);
  const asOf = (authority.fetched_at ?? "").trim();

  return (
    <li className="rounded-md border border-slate-200 bg-white px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-[14px] font-medium text-slate-900">{name}</span>
        {descriptor && <span className="text-[12px] text-slate-500">— {descriptor}</span>}
      </div>
      {(src || asOf) && (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-slate-500">
          {src && (
            <a
              href={src}
              target="_blank"
              rel="noopener noreferrer"
              className="text-slate-600 underline underline-offset-2 hover:text-slate-800"
            >
              Zdroj: {sourceHost(src)}
            </a>
          )}
          {asOf && <span className="text-slate-400">stav k {asOf}</span>}
        </div>
      )}
    </li>
  );
}

// The public renderer used by AnswerView. Returns null when there is no authority to
// show → the caller renders today's view byte-identically.
export function AuthoritiesBlock({ answer }: { answer: AnswerResponse }) {
  const authorities = (answer.authorities ?? []).filter(
    (a): a is Authority => Boolean(a && (a.name ?? "").toString().trim()),
  );
  if (authorities.length === 0) return null;

  return (
    <section
      className="mt-8 rounded-lg border border-slate-200 bg-slate-50 px-4 py-4"
      aria-label="Kam se obrátit"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600">
          Kam se obrátit
        </h2>
        <span className="inline-flex items-center rounded-full border border-slate-300 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-500">
          Praktická informace — není právní obsah
        </span>
      </div>
      <p className="mt-1 text-[12px] text-slate-400">
        Orientační kontaktní úřad z adresáře (návrh, ověřuje advokát). Neslouží jako
        právní zdroj ani citace ustanovení.
      </p>
      <ul className="mt-3 space-y-2">
        {authorities.map((a, i) => (
          <AuthorityRow key={`${a.authority_type}-${a.name}-${i}`} authority={a} />
        ))}
      </ul>
    </section>
  );
}
