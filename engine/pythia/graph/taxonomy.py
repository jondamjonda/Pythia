"""Canonical taxonomy of Czech and EU legal instruments: a seven-axis controlled vocabulary.

The single source of truth for classifying a legal instrument, following the registry pattern of
`relations.py`. Pure and database-free: the controlled vocabularies, the `document_type` registry
that drives classification, and the deterministic `classify()` function. The schema and population
live in `pythia/ingest/classify_taxonomy.py`, which builds on this module. Adding a new instrument
type means adding one registry row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# ─────────────────────────── Axis i: legal-force tier (ordered, highest first) ────────────────────
# The rank drives lex superior reasoning; EU instruments have a parallel rank space of their own.
LEGAL_FORCE_TIERS: dict[str, int] = {
    "CONSTITUTIONAL_ORDER": 1,
    "INTERNATIONAL_TREATY_HR": 2,
    "INTERNATIONAL_TREATY": 3,
    "STATUTE": 4,
    "GOV_REGULATION": 5,
    "MINISTERIAL_DECREE": 6,
    "MEASURE_GENERAL_NATURE": 7,
    "REGIONAL_MUNICIPAL_PREDPIS": 8,
    "INDIVIDUAL_ACT": 9,
    "SOFT_LAW": 10,
    "CASE_LAW": 11,
}
EU_TIERS: dict[str, int] = {"EU_PRIMARY": 1, "EU_SECONDARY": 2, "EU_SOFT": 3}
LEGAL_HIERARCHY = set(LEGAL_FORCE_TIERS) | set(EU_TIERS)

# ──────────────────────── Axis vi: bindingness (ordered, hardest first) ───────────────────────────
BINDINGNESS: tuple[str, ...] = (
    "HARD_LAW_ERGA_OMNES",
    "BINDING_OOP",
    "BINDING_IN_PERSONAM",
    "SOFT_LAW",
    "INTERPRETIVE_CASE_LAW",
)

# ─────────────────────────── Axis v: territorial scope ───────────────────────────
TERRITORIAL_SCOPE: tuple[str, ...] = ("EU", "INTERNATIONAL", "NATIONAL", "REGIONAL", "MUNICIPAL", "DISTRICT")

# ─────────────────────────── Axis vii: temporal status ───────────────────────────
TEMPORAL_STATUS: tuple[str, ...] = (
    "IN_FORCE",
    "NOT_YET_EFFECTIVE",
    "REPEALED",
    "CONSOLIDATED",
    "AMENDING",
    "ANNULLED_BY_CC",
)


# ─────────────────────── Axis ii: document type (the registry that drives classification) ─────────
@dataclass(frozen=True)
class DocType:
    key: str  # stable machine key
    cz: str  # Czech term
    en: str  # English gloss
    tier: str  # default legal-force tier (axis i)
    bindingness: str  # default bindingness (axis vi)
    issuer: str  # typical issuing authority (axis iii); "" = derive from context


# Each row supplies the *defaults* the classifier assigns from the document type alone.
DOC_TYPES: tuple[DocType, ...] = (
    DocType(
        "ustavni_zakon",
        "Ústavní zákon",
        "Constitutional act",
        "CONSTITUTIONAL_ORDER",
        "HARD_LAW_ERGA_OMNES",
        "Parlament",
    ),
    DocType("zakon", "Zákon", "Act / statute", "STATUTE", "HARD_LAW_ERGA_OMNES", "Parlament"),
    DocType("zakonik", "Zákoník / řád", "Code / procedural code", "STATUTE", "HARD_LAW_ERGA_OMNES", "Parlament"),
    DocType(
        "zakonne_opatreni",
        "Zákonné opatření Senátu",
        "Statutory measure of the Senate",
        "STATUTE",
        "HARD_LAW_ERGA_OMNES",
        "Senát",
    ),
    DocType(
        "narizeni_vlady", "Nařízení vlády", "Government regulation", "GOV_REGULATION", "HARD_LAW_ERGA_OMNES", "Vláda"
    ),
    DocType("vyhlaska", "Vyhláška", "Ministerial decree", "MINISTERIAL_DECREE", "HARD_LAW_ERGA_OMNES", ""),
    DocType("vyhlaska_cnb", "Vyhláška ČNB", "ČNB decree", "MINISTERIAL_DECREE", "HARD_LAW_ERGA_OMNES", "ČNB"),
    DocType("oop", "Opatření obecné povahy", "Measure of general nature", "MEASURE_GENERAL_NATURE", "BINDING_OOP", ""),
    DocType(
        "cenove_rozhodnuti",
        "Cenové rozhodnutí / výměr",
        "Price decision (OOP form)",
        "MEASURE_GENERAL_NATURE",
        "BINDING_OOP",
        "",
    ),
    DocType(
        "ozv",
        "Obecně závazná vyhláška",
        "Municipal ordinance (self-gov.)",
        "REGIONAL_MUNICIPAL_PREDPIS",
        "HARD_LAW_ERGA_OMNES",
        "",
    ),
    DocType(
        "narizeni_usc",
        "Nařízení obce / kraje",
        "Municipal/regional regulation (delegated)",
        "REGIONAL_MUNICIPAL_PREDPIS",
        "HARD_LAW_ERGA_OMNES",
        "",
    ),
    DocType("rozhodnuti", "Rozhodnutí", "Administrative decision", "INDIVIDUAL_ACT", "BINDING_IN_PERSONAM", ""),
    DocType("prikaz", "Příkaz", "Order (summary decision)", "INDIVIDUAL_ACT", "BINDING_IN_PERSONAM", ""),
    DocType("uredni_sdeleni", "Úřední sdělení", "Official communication", "SOFT_LAW", "SOFT_LAW", ""),
    DocType("vykladove_stanovisko", "Výkladové stanovisko", "Interpretive opinion", "SOFT_LAW", "SOFT_LAW", ""),
    DocType("metodicky_pokyn", "Metodický pokyn / metodika", "Methodological guidance", "SOFT_LAW", "SOFT_LAW", ""),
    DocType("oznameni", "Oznámení", "Notice", "SOFT_LAW", "SOFT_LAW", ""),
    DocType("sdeleni", "Sdělení", "Ministerial announcement / communication", "SOFT_LAW", "SOFT_LAW", ""),
    DocType("stanovisko", "Stanovisko", "Opinion / guideline", "SOFT_LAW", "SOFT_LAW", ""),
    DocType(
        "nalez_us",
        "Nález Ústavního soudu",
        "Constitutional Court ruling",
        "CASE_LAW",
        "INTERPRETIVE_CASE_LAW",
        "Ústavní soud",
    ),
    DocType(
        "usneseni_us",
        "Usnesení Ústavního soudu",
        "Constitutional Court resolution",
        "CASE_LAW",
        "INTERPRETIVE_CASE_LAW",
        "Ústavní soud",
    ),
    DocType("rozsudek", "Rozsudek / usnesení soudu", "Court judgment", "CASE_LAW", "INTERPRETIVE_CASE_LAW", ""),
    DocType(
        "sjednocujici_stanovisko",
        "Sjednocující stanovisko",
        "Unifying opinion",
        "CASE_LAW",
        "INTERPRETIVE_CASE_LAW",
        "",
    ),
    DocType(
        "mezinarodni_smlouva",
        "Mezinárodní smlouva",
        "International treaty",
        "INTERNATIONAL_TREATY",
        "HARD_LAW_ERGA_OMNES",
        "",
    ),
    DocType("eu_narizeni", "EU nařízení", "EU regulation", "EU_SECONDARY", "HARD_LAW_ERGA_OMNES", "EU"),
    DocType("eu_smernice", "EU směrnice", "EU directive", "EU_SECONDARY", "HARD_LAW_ERGA_OMNES", "EU"),
    DocType("eu_rozhodnuti", "EU rozhodnutí", "EU decision", "EU_SECONDARY", "HARD_LAW_ERGA_OMNES", "EU"),
    DocType("eu_doporuceni", "EU doporučení / stanovisko", "EU recommendation / opinion", "EU_SOFT", "SOFT_LAW", "EU"),
    DocType("eu_smlouva", "Zakládací smlouvy EU", "EU treaty", "EU_PRIMARY", "HARD_LAW_ERGA_OMNES", "EU"),
)
DOC_BY_KEY: dict[str, DocType] = {d.key: d for d in DOC_TYPES}

# Maps coarse `document_type`/`legal_hierarchy` values from early ingestion to a document-type key.
LEGACY_DOCTYPE_MAP: dict[str, str] = {
    "act": "zakon",
    "cz-act": "zakon",
    "directive": "eu_smernice",
    "eu-directive": "eu_smernice",
    "regulation": "eu_narizeni",
    "eu-regulation": "eu_narizeni",
}

# Axis iv: legal domain, as (branch, title-keyword regex) pairs; a document may match several
# branches. Financial-market law is listed first.
DOMAIN_CUES: tuple[tuple[str, str], ...] = (
    (
        "financial-market",
        "bank|banc|bance|úvěr|kapitál|investic|fond|pojišť|pojist|spoř|směn|platebn|"
        "dohledu|čnb|české národní bank|cenných papír|dluhopis|deriv|úpadk|přeměn|nabíd|"
        "oběh bankov|účetnictv|auditor|penzij|konglomerát|"
        # EU (English titles):
        "credit|capital market|invest|fund|insur|payment|securities|bond|"
        "crypto|markets in financial|aifm|mifi|deposit|prudential",
    ),
    ("aml", "praní|výnos|legaliz|terorism|sankc|money laundering|terroris|anti-money"),
    ("data-protection", "osobních údaj|gdpr|data protection|personal data|ochran.*údaj"),
    ("tax", "daň|dan[iíě]|daňov|spotřebních dan|přidané hodnoty"),
    ("criminal", "trestní|trestn[íéý]|trestu|protiprávní|mládež|probačn|odnětí svobody"),
    ("civil-procedure", "soudní řád|řízeních soudních|exeku|soudních poplat|mezinárodním právu soukrom"),
    ("procurement", "veřejných zakázek|zadávání|registru smluv|finanční kontrol|hospodářské soutěž|kontrolním úřad"),
    ("administrative", "správní řád|řád správní|přestup|kontrolní řád|státní služb"),
    ("labour", "zákoník práce|zaměstnan|inspekci práce|kolektivním vyjednáv|ochrany zdraví při práci|pracovněprávn"),
    # Further branches, which otherwise leave many documents uncategorized. Ordered from specific to
    # general and placed before the broad 'civil' (občansk|závazk), so a concrete branch is recorded
    # and civil stays the residual private-law catch-all. A document may carry several branches
    # (e.g. consumer and financial-market). Each pattern is unit-tested against the domain's
    # canonical Czech title and against regional or municipal ordinances (OZV, nařízení) whose
    # subject is in the title (e.g. 'o zřízení přírodní památky', 'úseky silnic').
    (
        "environment",
        "životní prostřed|přírodní památk|přírodní rezervac|chráněn.{0,3} území|ochran.{0,4}přírod|"
        "národní park|ovzduš|odpad|zákon.{0,4}o vodách|vodní zákon|ekologick|emis",
    ),
    # construction: anchored to building-law phrases so it does not match 'stavební spoření' (a
    # savings product).
    (
        "construction",
        "stavební zákon|stavebního zákon|stavební povolen|stavebních prac|územní plán|"
        "územního plánován|vyvlastněn|o výstavb",
    ),
    (
        "transport",
        "silnič|pozemních komunikac|úsek.{0,4}silnic|provozu na pozemních|dráh|drážn|železni|"
        "stání.{0,4}vozidel|tržní řád|tržním řád",
    ),
    ("energy", "energetick|elektřin|plynárenstv|teplárenstv|hospodaření energi|podporovaných zdroj"),
    # public-order: recurring municipal ordinance subjects (night quiet, alcohol and pyrotechnics
    # bans, gambling).
    (
        "public-order",
        "noční.{0,3}klid|zákazu konzum|veřejného pořádku|veřejném prostranství|odpalován|"
        "pyrotechni|hazardních her|loteri",
    ),
    ("intellectual-property", "autorsk|průmyslov.{0,4}vlastnictv|patent|ochrann.{0,4}znám|užitných vzor|vynález"),
    ("real-property", "katastr|nemovit|vlastnictví byt|pozemkov"),
    ("transparency", "svobodn.{0,5}přístup.{0,5}informac|registru smluv|střetu zájm"),
    (
        "social-security",
        "důchodov|sociálního zabezpeč|nemocenském pojištěn|sociální podpoř|sociální podpor|"
        "hmotné nouzi|sociálních služb",
    ),
    # health: 'ochraně zdraví' (dative) only, not 'ochrany zdraví při práci' (genitive), which is
    # labour above.
    ("health", "zdravotn|léčiv|ochrany veřejného zdraví|ochraně zdraví|návykov"),
    ("competition", "hospodářské soutěž|významné tržní síl"),
    ("consumer", "ochraně spotřebitel|spotřebitelsk"),
    ("civil", "občansk|závazk"),
    ("corporate", "obchodních korporac|korporát|firemn|business corporation|družst|obchodních společnost"),
)


# ─────────────────────────── Validation (axes i to vii) ───────────────────────────
def is_valid(axis: str, value: str | None) -> bool:
    """Return whether `value` is in the controlled vocabulary for `axis`; None (unset) is valid."""
    if value is None:
        return True
    return {
        "legal_hierarchy": value in LEGAL_HIERARCHY,
        "document_type": value in DOC_BY_KEY,
        "bindingness": value in BINDINGNESS,
        "territorial_scope": value in TERRITORIAL_SCOPE,
        "temporal_status": value in TEMPORAL_STATUS,
    }.get(axis, True)


# ─────────────────────────── The classifier (pure, deterministic, rule-based) ─────────────────────
# A municipal or regional issuer token. It anchors the local-government regulation (nařízení ÚSC)
# cue so it never matches a Czech-titled EU regulation ("Nařízení Evropského parlamentu a Rady (EU)
# ..."). `_KRAJ` separates REGIONAL from MUNICIPAL.
_USC_ISSUER = re.compile(r"\b(obc[eií]|měst[aoě]|městsk|kraj|hlavního města|statutárního města)")
_KRAJ = re.compile(r"\bkraj")


def _doctype_key(title: str, legacy_doctype: str | None, legacy_hierarchy: str | None) -> str:
    """Resolve the controlled document-type key from title cues, falling back to the legacy label.

    Title cues win, because they distinguish a code, decree or measure of general nature from a bare
    'act'.
    """
    t = (title or "").lower()
    # EU first, from the legacy label or the unambiguous English instrument title. The title cue
    # lets EU acts classify correctly even when their legacy label has already been normalised.
    legacy = (legacy_doctype or legacy_hierarchy or "").lower()
    if legacy in ("directive", "eu-directive", "eu_smernice", "smernice") or re.match(r"\s*directive\b", t):
        return "eu_smernice"
    if legacy in ("regulation", "eu-regulation", "eu_narizeni", "narizeni") or re.search(
        r"\bregulation\s*\((?:eu|ec|eec)\)", t
    ):
        return "eu_narizeni"
    if legacy in ("decision", "eu-decision", "eu_rozhodnuti") or re.search(r"\b(?:commission|council)\s+decision\b", t):
        return "eu_rozhodnuti"
    # Czech: the most specific cue wins.
    # Constitutional order: the explicit "ústavní zákon" form (e.g. 110/1998 "ústavní zákon o
    # bezpečnosti ČR") and the two named constitutional instruments whose titles lack that phrase,
    # the Constitution ("Ústava České republiky", 1/1993) and the Charter ("Listina základních práv
    # a svobod", 2/1993). Without these two cues both would fall through to the plain-statute
    # default and be ranked STATUTE, understating their force.
    if (
        "ústavní zákon" in t
        or t.startswith("ústava ")
        or t == "ústava"
        or "ústava české republiky" in t
        or "listina základních práv" in t
    ):
        return "ustavni_zakon"
    if "opatření obecné povahy" in t or "opatření obecné" in t:
        return "oop"
    if "cenové rozhodnutí" in t or "cenový výměr" in t:
        return "cenove_rozhodnuti"
    # Municipal and regional law (Sbírka právních předpisů ÚSC) goes before the generic vyhláška/řád
    # cues, which would otherwise classify "obecně závazná vyhláška" as vyhlaska and a municipal
    # "tržní řád" regulation as zakonik.
    if "obecně závazná vyhláška" in t:
        return "ozv"
    if "nařízení" in t and "vlády" not in t and _USC_ISSUER.search(t):
        return "narizeni_usc"
    if "vyhláška" in t:
        return "vyhlaska_cnb" if ("čnb" in t or "české národní banky" in t) else "vyhlaska"
    if "nařízení vlády" in t:
        return "narizeni_vlady"
    if "úřední sdělení" in t:
        return "uredni_sdeleni"
    if "metodick" in t:
        return "metodicky_pokyn"
    # Case law: court rulings construe statutes, so they are CASE_LAW, not SOFT_LAW. Order matters:
    # the unifying-opinion cue ('sjednocující stanovisko') must precede the plain soft-law
    # 'stanovisko' cues below, and the Constitutional Court cues precede the generic 'rozsudek' cue.
    if "nález" in t and "ústavního soudu" in t:
        return "nalez_us"
    if "usnesení" in t and "ústavního soudu" in t:
        return "usneseni_us"
    if "sjednocující stanovisko" in t:
        return "sjednocujici_stanovisko"
    if "rozsudek" in t and "soud" in t:
        return "rozsudek"
    # A general-court 'usnesení' (resolution). The Constitutional Court case is handled above, so a
    # title reaching here with both 'usnesení' and 'soud' is a lower-court resolution, which is also
    # CASE_LAW (the 'rozsudek' document type is "Rozsudek / usnesení soudu"). It comes after the
    # Constitutional Court and unifying-opinion cues and before the plain soft-law 'stanovisko' cue.
    if "usnesení" in t and "soud" in t:
        return "rozsudek"
    # Soft law: interpretive and methodological opinions. Placed after the 'sjednocující stanovisko'
    # case-law cue so a unifying opinion is not captured by the plain 'stanovisko' cue.
    if "výkladové stanovisko" in t or "stanovisko" in t:
        return "vykladove_stanovisko" if "výkladové stanovisko" in t else "stanovisko"
    if "zákoník" in t or t.strip().endswith(" řád") or " řád," in t or "soudní řád" in t or "trestní řád" in t:
        return "zakonik"
    if "zákonné opatření" in t:
        return "zakonne_opatreni"
    if "mezinárodní smlouva" in t or ("sdělení" in t and "o sjednání" in t):
        return "mezinarodni_smlouva"
    # A plain ministerial sdělení (announcement), e.g. the MPSV minimum-wage notices 286/2024 Sb.
    # and 356/2025 Sb.: non-binding SOFT_LAW. It comes after the 'úřední sdělení' cue
    # (uredni_sdeleni) and the treaty cue above ('sdělení ... o sjednání', mezinarodni_smlouva), so
    # this catch-all never captures the more specific forms.
    if "sdělení" in t:
        return "sdeleni"
    # Default: a plain statute.
    return LEGACY_DOCTYPE_MAP.get(legacy, "zakon")


def _domains(title: str) -> list[str]:
    import re

    t = (title or "").lower()
    out: list[str] = []
    for branch, pat in DOMAIN_CUES:
        if branch not in out and re.search(pat, t):
            out.append(branch)
    return out or ["uncategorized"]


def _temporal_status(valid_from: date | None, valid_to: date | None, as_of: date | None) -> str:
    ref = as_of or date.today()  # callers that need reproducible output pass an explicit as_of
    if valid_to is not None and valid_to <= ref:
        return "REPEALED"
    if valid_from is not None and valid_from > ref:
        return "NOT_YET_EFFECTIVE"
    return "IN_FORCE"


@dataclass
class Classification:
    legal_hierarchy: str
    document_type: str  # the controlled key
    bindingness: str
    territorial_scope: str
    temporal_status: str
    issuing_authority: str
    legal_domain: list[str] = field(default_factory=list)
    bindingness_contested: bool = False
    doctrinal_note: str | None = None


def classify(
    *,
    title: str,
    publisher: str | None = None,
    jurisdiction: str | None = None,
    legacy_doctype: str | None = None,
    legacy_hierarchy: str | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    as_of: date | None = None,
) -> Classification:
    """Classify a source document on the seven controlled-vocabulary axes (deterministic).

    The cue rules are sufficient for statutes and EU acts. Genuinely ambiguous instruments would be
    the place for an LLM fallback to refine the result.
    """
    key = _doctype_key(title, legacy_doctype, legacy_hierarchy)
    dt = DOC_BY_KEY[key]
    tier = dt.tier
    bindingness = dt.bindingness

    # Issuing authority: the registry default, else EU, ČNB or Parlament depending on context.
    issuer = dt.issuer
    if not issuer:
        if tier in EU_TIERS:
            issuer = "EU"
        elif "čnb" in (title or "").lower() or "české národní banky" in (title or "").lower():
            issuer = "ČNB"
        else:
            issuer = "Parlament" if tier == "STATUTE" else "neurčeno"

    # Territorial scope from the tier and jurisdiction.
    if tier in EU_TIERS or (jurisdiction or "").upper() == "EU":
        territorial = "EU"
    elif tier == "REGIONAL_MUNICIPAL_PREDPIS":
        territorial = "REGIONAL" if _KRAJ.search((title or "").lower()) else "MUNICIPAL"
    elif tier == "INTERNATIONAL_TREATY" or tier == "INTERNATIONAL_TREATY_HR":
        territorial = "INTERNATIONAL"
    else:
        territorial = "NATIONAL"

    # Flags for doctrinal uncertainty and contested bindingness.
    note = None
    contested = False
    if tier == "INTERNATIONAL_TREATY_HR":
        note = "HR-treaty rank (constitutional vs supra-statutory) is doctrinally contested — flag, do not assert."
    if bindingness == "SOFT_LAW":
        contested = True  # soft-law bindingness is a gradient (often followed, may be reclassified)

    return Classification(
        legal_hierarchy=tier,
        document_type=key,
        bindingness=bindingness,
        territorial_scope=territorial,
        temporal_status=_temporal_status(valid_from, valid_to, as_of),
        issuing_authority=issuer,
        legal_domain=_domains(title),
        bindingness_contested=contested,
        doctrinal_note=note,
    )
