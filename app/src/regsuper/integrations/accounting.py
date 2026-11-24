"""Accounting-export importers for POHODA and Money S3.

They turn a customer's already-uploaded accounting-export XML into a `BusinessProfileIntakeDraft`
(the pre-fill for the L0 intake form) and, for the round trip, render an information note back out
in POHODA's XML dialect. Not implemented yet: every callable raises NotImplementedError, nothing
is mounted in `api/main.py`, and `accounting_integration_enabled` stays off.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Design constraints for the implementation:
#   * Stdlib only. Parsing uses xml.etree.ElementTree; there is no SaaS connector and no network
#     access. The importers receive raw XML bytes that the upload endpoint has already stored.
#   * The draft is a plain value object, not an ORM row. BusinessProfileIntakeDraft mirrors a
#     subset of storage.models.BusinessProfile fields but writes nothing; the caller maps an
#     accepted draft onto the ORM.
#   * Derived L0 tags are never set here. derived_sector_tags and derived_activity_tags on
#     BusinessProfile are written only by L0 (see models.py); an import proposes only the
#     human-entered fields.

_WP = "WP-050 (Phase C accounting import)"


@dataclass(slots=True)
class BusinessProfileIntakeDraft:
    """A parsed but unsaved pre-fill for the L0 business-profile intake form.

    A plain value object, not `storage.models.BusinessProfile`. It carries the human-entered
    identity and scale fields an accounting export can supply, plus a free-form `source` tag and a
    `warnings` list that the UI shows so the customer can confirm or correct the values before
    anything is persisted. A draft is validated and mapped onto the `business_profile` row only
    on explicit acceptance.

    Field names match the corresponding `BusinessProfile` columns one to one, so that mapping is
    a straight copy. An import never populates the five compliance gates or the L0-only
    derived-tag columns.
    """

    # --- identity (mirrors BusinessProfile.{display_name,legal_name,ico,...}) ---
    display_name: str | None = None
    legal_name: str | None = None
    ico: str | None = None  # IČO 8-digit string
    legal_form: str | None = None  # sro|as|osvč|ks|vos|se|družstvo|other
    nace_code: str | None = None
    nace_description: str | None = None

    # --- scale / location ---
    headcount_band: str | None = None  # 1|2-9|10-49|50-249|250+
    annual_turnover_czk: int | None = None
    primary_jurisdiction: str | None = None  # CZ|EU|CZ+EU
    registered_municipality: str | None = None
    preferred_language: str | None = None  # cs|en

    # --- provenance and intake hints (not BusinessProfile columns) ---
    source: str | None = None  # 'pohoda' | 'money_s3'
    warnings: list[str] = field(default_factory=list)


def import_pohoda_xml(xml_bytes: bytes) -> BusinessProfileIntakeDraft:
    """Parse a POHODA accounting-export XML payload into an intake draft.

    POHODA exports use the namespaced `dat:dataPack` / `typ:*` XML dialect. The implementation
    will walk the address-book and company-identity block (`typ:ico`, `typ:company`, legal form,
    NACE) with `xml.etree.ElementTree` and return `BusinessProfileIntakeDraft(source='pohoda',
    ...)`, adding a `warnings` entry for every field it cannot resolve confidently. No network
    and no database write: bytes in, dataclass out.

    Args:
        xml_bytes: The raw, already-uploaded POHODA XML export (UTF-8 or Windows-1250).

    Returns:
        A draft for the customer to confirm before it is persisted.
    """
    raise NotImplementedError(_WP)


def import_money_s3_xml(xml_bytes: bytes) -> BusinessProfileIntakeDraft:
    """Parse a Money S3 accounting-export XML payload into an intake draft.

    Money S3 uses its own XML schema, with element names different from POHODA's. The
    implementation will extract the equivalent company-identity fields with
    `xml.etree.ElementTree` and return `BusinessProfileIntakeDraft(source='money_s3', ...)`, with
    a `warnings` entry per unresolved field. Stdlib only; no network and no database write.

    Args:
        xml_bytes: The raw, already-uploaded Money S3 XML export.

    Returns:
        A draft for the customer to confirm before it is persisted.
    """
    raise NotImplementedError(_WP)


def export_pohoda_note(draft: BusinessProfileIntakeDraft, *, note_text: str) -> bytes:
    """Render an information note in POHODA's XML dialect, for the round trip.

    The output is a POHODA-importable note document carrying `note_text`, which must be general
    legal information (lane='information'), never advice, so the customer can file it back into
    their accounting package. The implementation will build the `dat:dataPack` tree with
    `xml.etree.ElementTree` and serialise it; stdlib only, no network.

    Args:
        draft: The accepted profile draft the note is attached to.
        note_text: The information-lane note body to embed.

    Returns:
        Serialised POHODA XML bytes.
    """
    raise NotImplementedError(_WP)


__all__ = [
    "BusinessProfileIntakeDraft",
    "export_pohoda_note",
    "import_money_s3_xml",
    "import_pohoda_xml",
]
