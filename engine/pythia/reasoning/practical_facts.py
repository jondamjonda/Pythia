"""Deterministic extractor of practical facts (deadlines, authorities, penalties, fees and the like).

Regex-mines verbatim excerpts from a provision's full text and returns typed, span-anchored facts:

    fact_type: one of FACT_TYPES (deadline, authority, penalty, form, channel, fee, procedure_step,
        amount)
    value_raw: the exact substring copied out of `text`, never transformed, normalised or
        date-computed
    char_start, char_end: offsets into `text` such that `text[char_start:char_end] == value_raw`

At answer time these facts supply practical data to planning and drafting, and they are the
verbatim-substring ground truth for the V-FACT verifier. The extractor never computes dates, never
normalises amounts and never calls an LLM. It returns [] when nothing matches and is deterministic:
the same `text` always yields the same list, ordered by (char_start, char_end).

Why verbatim spans: a fact excerpt must be an exact slice of the provision's stored text so that (a)
the widened drafting context window can be built around the excerpt's offsets and (b) a verifier can
prove the excerpt is grounded by pure substring identity. Any transformation here would break both,
so the code only matches, slices and records offsets.

The consumer is answer.py's `_fetch`, which calls this only when `ctx_window_enabled` is on. The
module also provides term-match span mining, the second span source for that context window.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Public fact-type vocabulary: the subset of the practical_fact.fact_type enum that this module
# extracts.
FACT_TYPES = ("deadline", "authority", "penalty", "form", "channel", "fee", "procedure_step", "amount")


@dataclass(frozen=True)
class PracticalFact:
    """One verbatim fact, anchored to its span in the provision text.

    `text[char_start:char_end] == value_raw` holds by construction.
    """

    fact_type: str  # one of FACT_TYPES
    value_raw: str  # exact substring of the source provision text, never transformed
    char_start: int  # inclusive offset into the source text
    char_end: int  # exclusive offset into the source text

    def as_dict(self) -> dict:
        return {
            "fact_type": self.fact_type,
            "value_raw": self.value_raw,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


# ---------------------------------------------------------------------------------------------------------
# Patterns: each is a (fact_type, compiled regex) pair. Every regex matches a self-contained
# verbatim span of the provision's own text, and match.start(), match.end() and match.group(0) are
# recorded so the slice is exact. Czech legal vocabulary, case-insensitive and Unicode-aware so
# diacritics match. Deliberately conservative (precision over recall): a pattern must anchor on an
# unambiguous legal token, not merely a stray number.
# ---------------------------------------------------------------------------------------------------------

# Deadline: an explicit period or time-limit clause. Covers:
#   - "ve lhůtě 30 dnů", "lhůta 15 dní", "lhůtě do 3 měsíců"  (lhůt* plus a duration)
#   - "do 30 dnů", "do 15 dní ode dne", "do 3 pracovních dnů" (do N <time unit>)
#   - "nejpozději do 30 dnů"                                  (nejpozději do N <time unit>)
# The unit set is the Czech statutory time vocabulary (den/dní/dnů/pracovních dnů/týden/měsíc/rok in
# their inflected forms). The span stays tight around the clause so the excerpt is a usable quote.
_TIME_UNIT = (
    r"(?:pracovn(?:ího|ích)\s+)?"
    r"(?:dn[eů]|dní|dne|dnech|den|"
    r"t[ýy]dn[uůeí]*|t[ýy]dnech|"
    r"m[eě]s[ií]c[eů]*|m[eě]s[ií]c[uů]|m[eě]s[ií]ci|m[eě]s[ií]cích|"
    r"rok[uůyů]*|rok|let|roce|letech|"
    r"hodin[ay]?|hodin)"
)

_DEADLINE_PATTERNS = [
    # lhůt* … <number> <unit>   (the lhůta keyword carries a concrete duration)
    re.compile(r"lh[uů]t[a-zěí]*\s+(?:[^\d.;\n]{0,40}?)?(?:do\s+)?\d+\s*" + _TIME_UNIT, re.IGNORECASE | re.UNICODE),
    # nejpozději do <number> <unit>
    re.compile(r"nejpozd[eě]ji\s+do\s+\d+\.?\s*" + _TIME_UNIT, re.IGNORECASE | re.UNICODE),
    # do <number> <unit>: a bare "do N dnů" clause, the canonical explicit deadline
    re.compile(r"\bdo\s+\d+\s*" + _TIME_UNIT + r"(?:\s+ode\s+dne)?", re.IGNORECASE | re.UNICODE),
]

# Authority: a named competent office or body. A fixed lexicon of the Czech authorities relevant to
# small businesses; each entry is a verbatim phrase, with inflection tails where the noun declines,
# so it matches the nominative and the inflected forms seen in statute text ("správci daně", "úřadu
# práce"). Precision first: only unambiguous multi-word names and acronyms (a bare "úřad" is too
# generic and is not listed).
_AUTHORITY_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"spr[aá]vc[eiů]?\s+dan[eě]",  # správce daně / správci daně / správcem daně
        r"finan[cč]n[ií]m?\s+[uú][rř]ad[a-zěí]*",  # finanční úřad(u/em) …
        r"[uú][rř]ad[a-zěí]*\s+pr[aá]ce",  # úřad práce / úřadu práce
        r"[zž]ivnostensk[ýéáou]+\s+[uú][rř]ad[a-zěí]*",  # živnostenský úřad(u) …
        r"[cč]esk[aáéou]+\s+n[aá]rodn[ií]\s+bank[aouy]",  # Česká národní banka / ČNB (full form)
        r"\b[cČ]NB\b",  # ČNB (acronym)
        r"\bOSSZ\b",  # OSSZ (acronym)
        r"okresn[ií]\s+spr[aá]v[aou]\s+soci[aá]ln[ií]ho\s+zabezpe[cč]en[ií]",  # OSSZ full
        r"[cč]esk[aáéou]+\s+spr[aá]v[aou]\s+soci[aá]ln[ií]ho\s+zabezpe[cč]en[ií]",  # ČSSZ full
        r"\b[cČ]SSZ\b",  # ČSSZ acronym
        r"hygienick[aáéou]+\s+stanic[ei]?",  # hygienická stanice
        r"krajsk[aáéou]+\s+hygienick[aáéou]+\s+stanic[ei]?",  # krajská hygienická stanice
        r"inspektor[aá]t[a-zěí]*\s+pr[aá]ce",  # inspektorát práce
        r"[oú][rř]ad[a-zěí]*\s+pro\s+ochranu\s+osobn[ií]ch\s+[uú]daj[uů]",  # ÚOOÚ full
        r"\b[uÚ]OO[uÚ]\b",  # ÚOOÚ acronym
        r"katastr[aá]ln[ií]m?\s+[uú][rř]ad[a-zěí]*",  # katastrální úřad
        r"celn[ií]m?\s+[uú][rř]ad[a-zěí]*",  # celní úřad
    )
]

# Penalty: a fine clause. Matches:
#   - the pokut* keyword (pokuta, pokutu, pokuty, pokutou, …), optionally with an amount;
#   - an explicit CZK amount "… Kč" (for example "1 000 000 Kč" or "do 500000 Kč").
_PENALTY_PATTERNS = [
    # pokut* possibly followed by an amount clause "do 1 000 000 Kč"
    re.compile(
        r"pokut[a-zěí]*(?:\s+(?:a[žz]\s+)?do\s+v[yý][sš][eě]\s+)?"
        r"(?:[^.;\n]{0,30}?\d[\d\s.]*\s*(?:K[čc]|korun[a-zě]*|EUR))?",
        re.IGNORECASE | re.UNICODE,
    ),
    # a bare CZK amount clause anchored on Kč (catches amounts stated without the pokut* lead-in)
    re.compile(
        r"(?:a[žz]\s+)?do\s+(?:v[yý][sš][eě]\s+)?\d[\d\s.]*\s*(?:K[čc]|korun[a-zě]*)", re.IGNORECASE | re.UNICODE
    ),
]

# Form: a prescribed form, document or submission medium.
_FORM_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"tiskopis[a-zěí]*",  # tiskopis / tiskopisu / na tiskopise …
        r"formul[aá][rř][a-zěí]*",  # formulář / formuláře / na formuláři …
        r"datov[aáéou]+\s+schr[aá]nk[aouyeí]+",  # datová schránka / datovou schránkou …
        r"elektronick[yýéou]+\s+pod(?:pis|[aá]n[ií])[a-zěí]*",  # elektronický podpis/podání
        r"[uú][rř]edn[eě]\s+ov[eě][rř]en[yýéou]+",  # úředně ověřený (podpis)
    )
]

# Channel: how a filing or message reaches the office, as distinct from form (the physical medium or
# document). A fixed lexicon of Czech e-government and traditional channels, each a verbatim phrase.
# Precision first: anchor on an unambiguous channel token, not a stray adverb.
_CHANNEL_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"datov[aáéou]+\s+schr[aá]nk[aouyeí]+",  # datová schránka / datovou schránkou …
        r"prost[rř]ednictv[ií]m\s+datov[eéou]+\s+schr[aá]nk[yeí]+",  # prostřednictvím datové schránky
        r"elektronick[yýéou]+\s+pod[aá]n[ií][a-zěí]*",  # elektronické podání / elektronickým podáním
        r"elektronicky\b",  # elektronicky (adverb form)
        r"e-?mailem\b",  # e-mailem / emailem
        r"e-?mailov[eéou]+\s+zpr[aá]v[aouy]+",  # e-mailovou zprávou
        r"p[ií]semn[eě]\b",  # písemně
        r"osobn[eě]\b",  # osobně
        r"prost[rř]ednictv[ií]m\s+portál[a-zěů]*",  # prostřednictvím portálu …
        r"prost[rř]ednictv[ií]m\s+aplikace\s+[A-Za-zÀ-ž]+",  # prostřednictvím aplikace ePortál
        r"MOJE\s+dan[eě]",  # MOJE daně (portal name)
        r"[eE]Portál[a-zěů]*",  # ePortál (ČSSZ portal name)
        r"[dD]a[nň]ov[yýéou]+\s+portál[a-zěů]*",  # daňový portál
        r"form[oou]+\s+datov[eéou]+\s+zpr[aá]v[yeí]+",  # formou datové zprávy
    )
]

# Fee: an administrative fee or charge clause, as distinct from a penalty fine. Matches the fee
# keyword, a fee-amount clause, the "zpoplatněn" adjective and the "osvobozen od poplatku"
# exemption. Precision first.
_FEE_PATTERNS = [
    # správní poplatek [ve výši N Kč]: the canonical administrative-fee clause, amount optional
    re.compile(
        r"spr[aá]vn[ií]m?\s+poplat[eku][a-zěí]*"
        r"(?:\s+(?:ve\s+v[yý][sš]i\s+)?\d[\d\s.]*\s*(?:K[čc]|korun[a-zě]*))?",
        re.IGNORECASE | re.UNICODE,
    ),
    # a bare poplat* + amount "poplatek 200 Kč" / "poplatku ve výši 1 000 Kč" (fee, not the pokut*
    # fine)
    re.compile(
        r"poplat[eku][a-zěí]*\s+(?:ve\s+v[yý][sš]i\s+)?\d[\d\s.]*\s*(?:K[čc]|korun[a-zě]*)", re.IGNORECASE | re.UNICODE
    ),
    # zpoplatněn(o/a): the "is subject to a charge" adjective
    re.compile(r"zpoplatn[eě]n[a-zěí]*", re.IGNORECASE | re.UNICODE),
    # osvobozen(o/a) od poplatku: the fee-exemption clause
    re.compile(r"osvobozen[a-zěí]*\s+od\s+poplat[eku][a-zěí]*", re.IGNORECASE | re.UNICODE),
]

# Procedure step: a conservative procedural-obligation signal, an actor plus a filing or
# notification verb ("podá přihlášku", "oznámí správci daně", "zaregistruje se", "předloží doklad").
# The span stays tight around the verb and its immediate object (a short verbatim clause) instead of
# parsing the whole sentence. Precision first: anchor on the verb token and bound the trailing
# object, so the excerpt stays a usable single-clause quote.
_PROC_VERB = (
    r"(?:pod[aá]v[aá]|pod[aá]|ohl[aá][sš][ií]|ohla[sš]uje|"
    r"ozn[aá]m[ií]|oznamuje|zaregistruje|registruje|"
    r"p[rř]edlo[žz][ií]|p[rř]edkl[aá]d[aá]|dolo[žz][ií]|"
    r"po[žz][aá]d[aá]|[uú][cč]in[ií])"
)
# The object is an optional, length-bounded trailing clause: "[ se] <up to ~48 chars, no clause
# break>". It stops at the first clause or sentence delimiter (. ; : , newline) or at the cap, so
# the excerpt stays a single clause and does not swallow the next verb's clause. The optional "se"
# covers reflexive verbs.
_PROC_OBJECT = r"(?:\s+se)?(?:\s+[^.;:,\n]{1,48})?"
# The verb must be a whole word, not the prefix of a longer one: 'podá' matches, but the noun
# 'Podání' (which starts with 'Podá') and the infinitive 'učinit' (which starts with 'učini') must
# not. A negative lookahead for a following Czech letter is more reliable than \b across diacritics;
# re.IGNORECASE covers the upper-case forms.
_VERB_END = r"(?![a-záčďéěíňóřšťúůýž])"

_PROCEDURE_STEP_PATTERNS = [
    # <verb> [se] <short bounded object clause>
    re.compile(r"\b" + _PROC_VERB + _VERB_END + _PROC_OBJECT, re.IGNORECASE | re.UNICODE),
    # "ve lhůtě … učiní …" — a deadline-anchored procedural step
    re.compile(
        r"ve\s+lh[uů]t[a-zěí]*\s+[^.;:\n]{0,60}?[uú][cč]in[ií]" + _VERB_END + _PROC_OBJECT, re.IGNORECASE | re.UNICODE
    ),
]

# Amount: a bare, self-contained quantity of practical value that no other type captures:
#   - a CZK amount stated without a pokut* (penalty) or poplat*/správní poplatek/zpoplatn (fee)
#     lead-in, for example the travel-allowance rate "148 Kč, trvá-li …" or the per-km "5,80 Kč".
#     Penalties and fees already own the amounts in their lead-in clauses, so those are filtered out
#     below instead of being duplicated;
#   - a percentage rate, such as the income-tax "15 %" or "23 %";
#   - an hour or week threshold, such as the "40 hodin" working week or "28 týdnů" of leave;
#   - the minimum-wage "Koeficient … činí 0,NNN" clause (a bare decimal coefficient with no unit
#     token).
# Precision first: every span anchors on a digit plus an unambiguous unit token (Kč/korun, %,
# hodin/týden) or on the "koeficient … činí <decimal>" phrase, never on a stray number. A number may
# carry a Czech thousands space and a decimal comma ("500 000", "5,80", "0,422"), and the span
# slices back to value_raw exactly.
#
# Precision guard: a "pokut* … Kč" fine or a "poplatek … Kč" fee also matches the bare CZK pattern,
# which would duplicate the amount as a second, thinner `amount` fact. A money amount whose span
# lies inside a penalty or fee span is therefore suppressed (_suppress_penalty_fee_amounts).
# Non-money amounts (%, hours, coefficient) never collide and are always kept; a fine written as "…
# ve výši 15 %" is not a pokut*-led penalty and stays an amount. Net effect: "pokut* … 500 000 Kč"
# yields a penalty fact and no overlapping amount fact.
_AMOUNT_MONEY_RE = re.compile(r"\d[\d\s ]*(?:,\d+)?\s*(?:K[čc]|korun[a-zě]*)", re.IGNORECASE | re.UNICODE)
_AMOUNT_PATTERNS = [
    # CZK amount (the guard later drops those covered by a penalty or fee)
    _AMOUNT_MONEY_RE,
    # percentage rate: "15 %", "23 %", "0,5 %"
    re.compile(r"\d[\d\s ]*(?:,\d+)?\s*%", re.IGNORECASE | re.UNICODE),
    # hour or week threshold: "40 hodin", "28 týdnů", "5 až 12 hodin" (the trailing unit anchors it)
    re.compile(r"\d[\d\s ]*(?:,\d+)?\s*(?:hodin[a-zě]*|t[ýy]dn[a-zěů]*)", re.IGNORECASE | re.UNICODE),
    # minimum-wage coefficient clause: "Koeficient … činí 0,422" (bare decimal, no unit token)
    re.compile(r"[Kk]oeficient[^.;:\n]*?[čc]in[íi]\s+\d+(?:,\d+)?", re.IGNORECASE | re.UNICODE),
]

_PATTERNS: list[tuple[str, re.Pattern[str]]] = (
    [("deadline", p) for p in _DEADLINE_PATTERNS]
    + [("authority", p) for p in _AUTHORITY_PATTERNS]
    + [("penalty", p) for p in _PENALTY_PATTERNS]
    + [("form", p) for p in _FORM_PATTERNS]
    + [("channel", p) for p in _CHANNEL_PATTERNS]
    + [("fee", p) for p in _FEE_PATTERNS]
    + [("procedure_step", p) for p in _PROCEDURE_STEP_PATTERNS]
    + [("amount", p) for p in _AMOUNT_PATTERNS]
)


def _suppress_penalty_fee_amounts(facts: list[PracticalFact]) -> list[PracticalFact]:
    """Drop money amounts that duplicate a penalty or fee.

    An `amount` fact in Kč/korun whose span lies inside a penalty or fee span is removed, so a
    "pokut* … 500 000 Kč" fine or a "poplatek … 200 Kč" fee is not emitted again as a thinner
    amount. Only money amounts can collide (both anchor on Kč); percentage, hour/week and
    coefficient amounts are always kept. `facts` must already be sorted.
    """
    covers = [(f.char_start, f.char_end) for f in facts if f.fact_type in ("penalty", "fee")]
    if not covers:
        return facts
    kept: list[PracticalFact] = []
    for f in facts:
        if (
            f.fact_type == "amount"
            and _AMOUNT_MONEY_RE.fullmatch(f.value_raw)
            and any(cs <= f.char_start and f.char_end <= ce for cs, ce in covers)
        ):
            continue
        kept.append(f)
    return kept


def _dedupe_overlaps(facts: list[PracticalFact]) -> list[PracticalFact]:
    """Drop a fact nested inside another fact of the same type, keeping the longer clause.

    Cross-type overlaps are kept: a penalty amount inside a deadline sentence is two distinct facts.
    Deterministic; the input is already sorted by (start, end).
    """
    kept: list[PracticalFact] = []
    for f in facts:
        contained = any(
            k.fact_type == f.fact_type
            and k.char_start <= f.char_start
            and f.char_end <= k.char_end
            and (k.char_start, k.char_end) != (f.char_start, f.char_end)
            for k in kept
        )
        if not contained:
            kept.append(f)
    return kept


def extract_practical_facts(text: str | None) -> list[PracticalFact]:
    """Mine verbatim, span-anchored practical facts from a provision's full text.

    Returns [] for empty or None input, or when no pattern matches. Every returned fact satisfies
    `text[f.char_start:f.char_end] == f.value_raw`. Facts are ordered by (char_start, char_end), and
    fully nested spans of the same type are de-duplicated (the longest is kept). Pure and
    deterministic: no LLM, no database, no date computation.
    """
    if not text:
        return []
    out: list[PracticalFact] = []
    seen: set[tuple[str, int, int]] = set()
    for fact_type, pat in _PATTERNS:
        for m in pat.finditer(text):
            start, end = m.start(), m.end()
            if end <= start:
                continue
            raw = text[start:end]
            # Trim surrounding whitespace without transforming the retained substring: recompute
            # tight offsets so the span still slices to value_raw exactly.
            lstrip = len(raw) - len(raw.lstrip())
            rstrip = len(raw) - len(raw.rstrip())
            start += lstrip
            end -= rstrip
            if end <= start:
                continue
            raw = text[start:end]
            key = (fact_type, start, end)
            if key in seen:
                continue
            seen.add(key)
            out.append(PracticalFact(fact_type=fact_type, value_raw=raw, char_start=start, char_end=end))
    out.sort(key=lambda f: (f.char_start, f.char_end))
    return _suppress_penalty_fee_amounts(_dedupe_overlaps(out))


# ---------------------------------------------------------------------------------------------------------
# Term-match span mining: the second span source for the context window. It finds diacritic- and
# case-folded occurrences of the query and sub-query terms in the full text, which catches a
# normative rule that has no practical-fact token but does contain the query's statute terms (for
# example the cookie rule in 127/2005 §89, around character 1278). Pure and deterministic; returns
# (start, end) spans into the original text.
# ---------------------------------------------------------------------------------------------------------


def _fold(s: str) -> str:
    """Case-fold and strip diacritics while keeping a 1:1 length map to the original text.

    NFD decomposition can change the length, so each character is folded on its own (decompose, keep
    the base character, lowercase) to exactly one character, which keeps offsets aligned with the
    source.
    """
    out_chars = []
    for ch in s:
        base = unicodedata.normalize("NFD", ch)
        # Keep only the non-combining code points; if decomposition expanded, take the first one so
        # the folded string stays 1:1 with the source index.
        stripped = "".join(c for c in base if not unicodedata.combining(c))
        out_chars.append((stripped[:1] or ch).lower())
    return "".join(out_chars)


# Folded (diacritic-stripped, lowercase) Czech function words and legal boilerplate. A
# context-window term source must not anchor on these: they occur in nearly every provision, so
# windowing around them floods the head of the text and degenerates ctx_txt to text[:cap], starving
# practical-fact spans beyond the head (a deadline past about 1,600 characters would be silently
# lost). Content terms (entity, action and deadline nouns) survive and remain the window anchors.
_CTX_STOPWORDS: frozenset[str] = frozenset(
    {
        # conjunctions, prepositions and particles of 3+ characters (shorter words fail the length
        # check)
        "nebo",
        "ale",
        "ani",
        "nez",
        "pak",
        "tak",
        "tam",
        "tim",
        "jak",
        "kde",
        "kdo",
        "coz",
        "jez",
        "pri",
        "pred",
        "pres",
        "pod",
        "nad",
        "mezi",
        "bez",
        "kvuli",
        "vuci",
        "krome",
        "misto",
        "behem",
        "diky",
        "aby",
        "pokud",
        "kdyz",
        "jako",
        "tento",
        "tato",
        "toto",
        "tomu",
        "teto",
        "techto",
        "vsak",
        "take",
        "jen",
        "jeho",
        "jejich",
        "byt",
        "byl",
        "byla",
        "bylo",
        "jsou",
        "musi",
        "muze",
        "ktery",
        "ktera",
        "ktere",
        "kterych",
        "kterym",
        # legal boilerplate that appears in almost every provision
        "zakon",
        "zakona",
        "zakonu",
        "zakonem",
        "odst",
        "pismeno",
        "pismena",
        "pism",
        "ustanoveni",
        "paragraf",
        "clanek",
        "podle",
        "dle",
        "pripade",
        "souladu",
        "vyse",
        "nize",
        "dalsi",
        "timto",
    }
)


def _tokens(terms: list[str] | None) -> list[str]:
    """Fold and split the leg or sub-query strings into distinct alphanumeric content terms (3+ chars).

    The result is de-duplicated in order. Function words and legal boilerplate (_CTX_STOPWORDS) are
    dropped so a term source anchors only on distinctive content; otherwise context windowing
    degenerates to the head slice.
    """
    if not terms:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        for tok in re.split(r"[^0-9A-Za-zÀ-ž]+", _fold(t or "")):
            if len(tok) >= 3 and tok not in seen and tok not in _CTX_STOPWORDS:
                seen.add(tok)
                out.append(tok)
    return out


def term_match_spans(text: str | None, terms: list[str] | None) -> list[tuple[int, int]]:
    """Find (start, end) spans in `text` where a folded query term of 3+ characters occurs.

    Matching ignores diacritics and case. Offsets index the original text (the fold preserves
    length). Returns a sorted list of non-overlapping spans.
    """
    if not text or not terms:
        return []
    folded = _fold(text)
    toks = _tokens(terms)
    if not toks:
        return []
    # Match whole words only, so 'dnu' does not match inside 'bezodkladneho' and the anchor spans
    # stay precise instead of flooding the text.
    spans: list[tuple[int, int]] = []
    for tok in toks:
        for m in re.finditer(r"\b" + re.escape(tok) + r"\b", folded):
            spans.append((m.start(), m.end()))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping or touching (start, end) spans into a minimal disjoint set."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged
