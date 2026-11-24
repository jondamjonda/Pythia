"""User-facing copy (Czech and English) for answers the system will not give with confidence.

Covers the four non-sound routes (not a legal question, not in the corpus, false premise,
refusal), the hedge banner for lower-confidence answers and the disclaimer chain stamped on every
answer. `api/answer.py` and the frontend render these strings verbatim. The wording is seed copy
that must be signed off by a lawyer before a paid launch. Pure data and one lookup; no I/O.
"""

from __future__ import annotations

from typing import Final, Literal

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
# The non-sound presentation routes: the question gate's render modes plus the refusal from the
# post-generation verification. "sound" is deliberately absent: a sound answer renders its own
# synthesized sections, and the hedge banner is the only thing a sound but hedged answer takes
# from this table.
Lang = Literal["cs", "en"]

ROUTE_NOT_A_LEGAL_QUESTION: Final[str] = "not_a_legal_question"
ROUTE_NOT_IN_CORPUS: Final[str] = "not_in_corpus"
ROUTE_FALSE_PREMISE: Final[str] = "false_premise"
ROUTE_REFUSE: Final[str] = "refuse"

# The four routes, in the order in which the question gate checks them. Each maps to a gate
# render mode: notlegal (the question is not legal), closest (no or weak basis in the corpus),
# falsepremise (the premise is corrected) and refuse (rejected by post-generation verification).
# The machine-readable counterpart that API integrators switch on is the RefusalReason enum in
# conduit/envelope.py; this table is the human-facing side.
NON_SOUND_ROUTES: Final[tuple[str, ...]] = (
    ROUTE_NOT_A_LEGAL_QUESTION,
    ROUTE_NOT_IN_CORPUS,
    ROUTE_FALSE_PREMISE,
    ROUTE_REFUSE,
)

# Keys that are not routes but whose copy also lives in this table: the hedge banner (an amber
# "lower confidence, verify with a professional" strip on a hedged answer) and the disclaimer
# chain ("information, not advice", plus point-in-time and unverified-citation caveats).
KEY_HEDGE_BANNER: Final[str] = "hedge_banner"
KEY_DISCLAIMER: Final[str] = "disclaimer"
KEY_DISCLAIMER_AS_OF_UNKNOWN: Final[str] = "disclaimer_as_of_unknown"
KEY_DISCLAIMER_UNVERIFIED_CITATIONS: Final[str] = "disclaimer_unverified_citations"

# Fallback for an unknown key: the lookup never raises KeyError, because an unknown route must
# degrade to the safest honest frame rather than crash the answer endpoint.
_FALLBACK_KEY: Final[str] = ROUTE_REFUSE

# Languages every key must carry. Czech is the primary product language.
SUPPORTED_LANGS: Final[tuple[Lang, ...]] = ("cs", "en")
_DEFAULT_LANG: Final[Lang] = "cs"


# ---------------------------------------------------------------------------
# The copy table — {key: {lang: {title, body, disclaimer}}}
# ---------------------------------------------------------------------------
# Every entry has the same shape: title (a short heading), body (the user-facing explanation) and
# disclaimer (the trailing caveat). The uniform shape lets the backend and frontend render any key
# with one code path. The constant "information, not advice" line lives in KEY_DISCLAIMER and is
# repeated in each entry's `disclaimer`, so every entry is self-contained.
_INFO_NOT_ADVICE_CS: Final[str] = (
    "Toto je obecná právní informace, nikoli právní rada k vašemu konkrétnímu "
    "případu. Pro závazné posouzení se obraťte na advokáta."
)
_INFO_NOT_ADVICE_EN: Final[str] = (
    "This is general legal information, not legal advice for your specific "
    "situation. For a binding assessment, consult a lawyer."
)

_TABLE: Final[dict[str, dict[Lang, dict[str, str]]]] = {
    ROUTE_NOT_A_LEGAL_QUESTION: {
        "cs": {
            "title": "Tohle nevypadá jako právní dotaz",
            "body": (
                "Váš dotaz se nezdá být právní otázkou, na kterou umíme odpovědět z "
                "české a evropské legislativy. Zkuste jej prosím přeformulovat na "
                "konkrétní právní otázku (například s odkazem na povinnost, zákon "
                "nebo § předpisu)."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "This does not look like a legal question",
            "body": (
                "Your query does not appear to be a legal question we can answer from "
                "Czech and EU legislation. Please try rephrasing it as a specific "
                "legal question (for example, referencing an obligation, an act, or a "
                "section of a regulation)."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    ROUTE_NOT_IN_CORPUS: {
        "cs": {
            "title": "K tomuto tématu nemáme dostatečný podklad",
            "body": (
                "V naší databázi předpisů jsme nenašli dostatečný podklad, abychom na "
                "tuto otázku odpovědně odpověděli. Níže uvádíme nejbližší související "
                "předpisy — nejsou však přímo k věci, a proto z nich neodvozujeme "
                "závěr. Neuvádíme nic, co nemůžeme doložit citací."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "We do not have enough basis for this topic",
            "body": (
                "We could not find sufficient basis in our regulatory database to "
                "answer this question responsibly. Below are the closest related "
                "provisions — but they are not directly on point, so we draw no "
                "conclusion from them. We state nothing we cannot back with a citation."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    ROUTE_FALSE_PREMISE: {
        "cs": {
            "title": "Pozor: dotaz vychází z nepřesného předpokladu",
            "body": (
                "Zdá se, že váš dotaz vychází z předpokladu, který neodpovídá platné "
                "právní úpravě. Níže uvádíme opravu vycházející z konkrétních "
                "předpisů, které ji dokládají, spolu s tím, co z nich skutečně plyne."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "Note: this question rests on an inaccurate premise",
            "body": (
                "Your question appears to rest on a premise that does not match the "
                "applicable law. Below is a correction grounded in the specific "
                "provisions that support it, together with what actually follows from "
                "them."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    ROUTE_REFUSE: {
        "cs": {
            "title": "Na tento dotaz nemůžeme spolehlivě odpovědět",
            "body": (
                "Nepodařilo se nám sestavit odpověď, kterou bychom dokázali plně "
                "doložit citacemi z předpisů. Abychom se vyhnuli nepodloženému "
                "tvrzení, raději neodpovídáme. Zkuste prosím dotaz zúžit nebo "
                "přeformulovat."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "We cannot answer this reliably",
            "body": (
                "We could not assemble an answer we can fully back with citations from "
                "the regulations. To avoid an unsupported statement, we decline to "
                "answer. Please try narrowing or rephrasing the question."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    KEY_HEDGE_BANNER: {
        "cs": {
            "title": "Nižší jistota",
            "body": (
                "Tuto odpověď uvádíme s nižší mírou jistoty — naše databáze toto téma "
                "pokrývá jen částečně. Závěr si prosím ověřte u odborníka."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "Lower confidence",
            "body": (
                "We present this answer with lower confidence — our database covers "
                "this topic only partially. Please verify the conclusion with a "
                "professional."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    KEY_DISCLAIMER: {
        "cs": {
            "title": "Upozornění",
            "body": _INFO_NOT_ADVICE_CS,
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "Disclaimer",
            "body": _INFO_NOT_ADVICE_EN,
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    # Some EU acts in the corpus have no recorded effective date. A provision whose valid_from is
    # NULL is treated as in force, but a date is never invented; this caveat is shown instead.
    KEY_DISCLAIMER_AS_OF_UNKNOWN: {
        "cs": {
            "title": "Datum účinnosti neuvedeno",
            "body": (
                "U některých citovaných předpisů není v databázi uvedeno datum "
                "účinnosti; považujeme je za účinné, ale datum neuvádíme."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "Effective date not stated",
            "body": (
                "For some cited provisions the effective date is not recorded in the "
                "database; we treat them as in force but do not state a date."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
    KEY_DISCLAIMER_UNVERIFIED_CITATIONS: {
        "cs": {
            "title": "Neověřené citace",
            "body": (
                "Tato odpověď obsahuje citace, které nebyly ověřeny právníkem. "
                "Ověřte si je prosím u advokáta."
            ),
            "disclaimer": _INFO_NOT_ADVICE_CS,
        },
        "en": {
            "title": "Unverified citations",
            "body": (
                "This answer contains citations that have not been reviewed by a "
                "lawyer. Please verify them with a lawyer."
            ),
            "disclaimer": _INFO_NOT_ADVICE_EN,
        },
    },
}


def route_copy(route: str, lang: str = _DEFAULT_LANG) -> dict[str, str]:
    """Return the copy for ``route`` in ``lang`` as ``{"title", "body", "disclaimer"}``.

    All three values are non-empty strings, and the lookup never raises ``KeyError``:

    * an unknown ``route`` falls back to the safest frame (``refuse``), so an unrecognised route
      produces an honest non-answer instead of crashing the answer endpoint;
    * an unsupported ``lang`` falls back to the primary language (``cs``).
    """
    entry = _TABLE.get(route, _TABLE[_FALLBACK_KEY])
    chosen: Lang = lang if lang in SUPPORTED_LANGS else _DEFAULT_LANG  # type: ignore[assignment]
    return dict(entry[chosen])  # a copy, so callers cannot mutate the table


def all_copy_keys() -> list[str]:
    """Return every key the table supplies: routes, hedge banner and disclaimer chain.

    In deterministic insertion order. Lets a parity test check that the table is complete
    without reaching into the private ``_TABLE``.
    """
    return list(_TABLE.keys())


__all__ = [
    "KEY_DISCLAIMER",
    "KEY_DISCLAIMER_AS_OF_UNKNOWN",
    "KEY_DISCLAIMER_UNVERIFIED_CITATIONS",
    "KEY_HEDGE_BANNER",
    "NON_SOUND_ROUTES",
    "ROUTE_FALSE_PREMISE",
    "ROUTE_NOT_A_LEGAL_QUESTION",
    "ROUTE_NOT_IN_CORPUS",
    "ROUTE_REFUSE",
    "SUPPORTED_LANGS",
    "all_copy_keys",
    "route_copy",
]
