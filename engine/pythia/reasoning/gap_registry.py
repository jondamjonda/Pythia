"""Pre-written Czech copy for the kinds of information the corpus is known not to contain.

The seven-slot action plan (reasoning/structured_answer.py) has slots that statute text alone cannot
fill: which office to walk into ("Kam se obrátit"), soft-law guidance, the online form or portal,
regional specifics and case law. Letting the LLM invent that content would be ungrounded and, for a
small business, dangerously wrong. Instead those slots are filled from this registry with an honest
"the corpus does not cover this; here is where to look" line. The same copy replaces a claim slot
that slot_verify demoted for lack of grounding.

Every string is a draft pending review by a lawyer. The copy is descriptive and cautious: it states
the corpus limit and where to look in general, and never states a specific obligation, § number or
authority decision, or acts on the reader's behalf. Lawyer sign-off is required before any of it is
enforced or served live; none of it is presented as legal advice.

Pure: a frozen dict of copy and a few accessors. Used only when ``structured_answer_enabled`` is on.
"""

from __future__ import annotations

# Classes of information the corpus is known not to contain, each with one draft Czech line. The
# names follow the "what the corpus does not cover" taxonomy: authority locator, soft law, form
# portal, regional rules and case law.
GAP_CLASSES = (
    "authority_locator",  # which branch office to contact (the corpus names the body, not the branch)
    "soft_law_guidance",  # methodické pokyny, stanoviska (GFŘ, ČNB, ÚOOÚ); mostly outside the corpus
    "form_portal",  # the online form, portal or datová schránka used to file
    "regional",  # municipal or regional rules (local decrees, municipal specifics)
    "case_law",  # court rulings interpreting the rule (thinly covered in the corpus)
)

# Draft Czech copy pending lawyer review, one line per class. Descriptive only: no § and no
# obligation.
_GAP_COPY: dict[str, str] = {
    "authority_locator": (
        "Korpus uvádí příslušný typ úřadu, ale ne konkrétní místně příslušné pracoviště. "
        "Konkrétní pobočku či podání ověřte přímo u příslušného úřadu nebo na portálu veřejné správy."
    ),
    "soft_law_guidance": (
        "Metodické pokyny, stanoviska a výkladová vodítka dozorových orgánů nejsou v korpusu plně obsaženy. "
        "Pro praktický postup doporučujeme ověřit aktuální metodiku příslušného úřadu."
    ),
    "form_portal": (
        "Konkrétní formulář, elektronické podání či datová schránka nejsou v textu předpisů uvedeny. "
        "Podací kanál a formulář najdete na stránkách příslušného úřadu."
    ),
    "regional": (
        "Místní (obecní/krajské) předpisy a specifika nejsou v korpusu plně pokryty. "
        "Ověřte případné místní vyhlášky u příslušné obce nebo kraje."
    ),
    "case_law": (
        "Relevantní soudní rozhodnutí k tomuto tématu nejsou v korpusu dostatečně zastoupena. "
        "Výklad soudů může upřesnit uplatnění pravidla."
    ),
}

# Line used when slot_verify demoted a claim slot (its prose had no grounded citation, or carried an
# unverifiable deadline or authority). Draft pending lawyer review.
_DEMOTED_LINE = (
    "K tomuto bodu se v korpusu nepodařilo dohledat dostatečně podložené ustanovení; "
    "informaci prosím ověřte u příslušného úřadu nebo právníka."
)

# Machine-readable marker on every emitted gap line, so the UI can badge it as pending review and
# never present it as a grounded statement.
DRAFT_MARKER = "DRAFT/advocate-review-pending"


def gap_copy(gap_class: str) -> str | None:
    """Return the draft Czech line for a gap class, or None for an unknown class."""
    return _GAP_COPY.get(gap_class)


def demoted_line() -> str:
    """Return the draft line that replaces a claim slot slot_verify demoted for lack of grounding."""
    return _DEMOTED_LINE


def gap_items(classes: list[str] | None = None) -> list[dict]:
    """Return gap items for the given classes (default: all known classes).

    Each item is ``{gap_class, text, status}`` with ``status`` always ``DRAFT_MARKER``. Unknown
    classes are skipped.
    """
    wanted = list(classes) if classes else list(GAP_CLASSES)
    out: list[dict] = []
    for c in wanted:
        copy = _GAP_COPY.get(c)
        if copy is None:
            continue
        out.append({"gap_class": c, "text": copy, "status": DRAFT_MARKER})
    return out
